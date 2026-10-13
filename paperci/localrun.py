"""Run PaperCI on your own machine: preflight checks for Docker, the sandbox images and your LLM endpoint.

  python -m paperci.localrun check [--build]

Then start the same agent the cloud runs: `python -m paperci.web` (http://127.0.0.1:8000).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

from .config import MODELS, PROXY_IMAGE, ROOT, SANDBOX_IMAGE

OK, WARN, FAIL = "ok  ", "warn", "FAIL"


def _run(cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


def check_docker(build: bool) -> list[tuple[str, str]]:
    out = []
    try:
        p = _run(["docker", "info", "--format", "{{.ServerVersion}} {{.OSType}}"])
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return [(FAIL, "Docker not found or not responding. Install Docker Desktop (Windows/macOS) or Docker Engine (Linux).")]
    if p.returncode != 0:
        return [(FAIL, "Docker is installed but the engine is not running. Start Docker Desktop / dockerd.")]
    ver, _, ostype = p.stdout.strip().partition(" ")
    out.append((OK if ostype == "linux" else FAIL, f"Docker {ver} ({ostype} containers)"
                + ("" if ostype == "linux" else ": switch Docker Desktop to Linux containers")))
    for image, ctx in ((PROXY_IMAGE, "sandbox/proxy"), (SANDBOX_IMAGE, "sandbox/runner")):
        have = _run(["docker", "image", "inspect", image]).returncode == 0
        if not have and build:
            print(f"building {image} from {ctx} (first time only, a few minutes)...", flush=True)
            have = _run(["docker", "build", "-q", "-t", image, str(ROOT / ctx)], timeout=3600).returncode == 0
        out.append((OK if have else FAIL, f"image {image}" + ("" if have else f" missing: docker build -t {image} {ctx}")))
    return out


def check_llm() -> list[tuple[str, str]]:
    out = []
    local = sorted({m for m in MODELS.values() if m.startswith("local:")})
    other = sorted({m for m in MODELS.values() if not m.startswith("local:")})
    for m in other:
        if m.startswith("factory:"):
            ok = bool(os.getenv("FACTORY_API_KEY"))
            out.append((OK if ok else FAIL, f"{m}: " + ("FACTORY_API_KEY set" if ok else "needs FACTORY_API_KEY")))
        else:
            ok = bool(os.getenv("LITELLM_API_KEY"))
            out.append((OK if ok else FAIL, f"{m}: " + ("LITELLM_API_KEY set" if ok else
                        "needs LITELLM_API_KEY, or use a local model: PAPERCI_MODEL_ALL=local:<model>")))
    if local:
        out += _check_local(local)
    if MODELS["builder"] == MODELS["verifier"]:
        out.append((WARN, f"builder and verifier are both {MODELS['builder']}. PaperCI is designed so a different model "
                          "judges the builder's work; set PAPERCI_MODEL_VERIFIER to another local model if you can."))
    return out


def _check_local(models: list[str]) -> list[tuple[str, str]]:
    from . import llm
    base = os.getenv("PAPERCI_LOCAL_BASE_URL", "http://localhost:11434/v1")
    try:
        served = {m.id for m in llm.local_client().models.list().data}
    except Exception as e:
        return [(FAIL, f"no OpenAI-compatible server at {base} ({type(e).__name__}). Start Ollama, LM Studio, vLLM or a "
                       "LiteLLM proxy, or set PAPERCI_LOCAL_BASE_URL.")]
    out = [(OK, f"LLM server at {base} serves {len(served)} model(s)")]
    for m in models:
        name = m[len("local:"):]
        if served and name not in served:
            out.append((FAIL, f"{m}: not served there. Available: {', '.join(sorted(served)[:8])}"))
            continue
        out.append(_tool_probe(m))
        out.append(_context_probe(m))
    return out


NEED_CTX = int(os.getenv("PAPERCI_LOCAL_MIN_CONTEXT", "16000"))


def _context_probe(model: str) -> tuple[str, str]:
    """Paper prompts run 10-20k tokens. LM Studio rejects them past the loaded context; Ollama silently keeps
    only the tail, so the reported prompt size is checked too."""
    from . import llm
    filler = "alpha " * NEED_CTX
    msgs = [{"role": "user", "content": filler + "\nReply with the single word: ok"}]
    fix = (f"load the model with a larger context (32768 recommended): LM Studio `lms load <model> -c 32768`, "
           f"Ollama `OLLAMA_CONTEXT_LENGTH=32768 ollama serve`, vLLM `--max-model-len 32768`")
    try:
        r = llm._local_chat({"model": model, "messages": msgs, "max_tokens": 5, "temperature": 0})
    except Exception as e:
        if "context" in str(e).lower() or "token" in str(e).lower():
            return (FAIL, f"{model}: context window is smaller than a paper prompt (~{NEED_CTX} tokens); {fix}")
        return (WARN, f"{model}: long-prompt probe failed ({type(e).__name__}: {str(e)[:120]})")
    seen = int(getattr(r.usage, "prompt_tokens", 0) or 0) if getattr(r, "usage", None) else 0
    if seen and seen < NEED_CTX * 0.8:
        return (FAIL, f"{model}: the server kept only {seen} of ~{NEED_CTX} prompt tokens (silent truncation); {fix}")
    return (OK, f"{model}: context holds a {NEED_CTX}-token prompt" + (f" ({seen} tokens seen)" if seen else ""))


def _tool_probe(model: str) -> tuple[str, str]:
    """The builder drives the sandbox through tool calls; check the model actually makes one."""
    from . import llm
    tools = [{"type": "function", "function": {"name": "shell", "description": "Run a shell command in the sandbox.",
                                               "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}},
                                                              "required": ["cmd"]}}}]
    msgs = [{"role": "system", "content": "You operate a Linux sandbox through the shell tool."},
            {"role": "user", "content": "List the files in the current directory."}]
    try:
        r = llm._local_chat({"model": model, "messages": msgs, "tools": tools, "max_tokens": 300, "temperature": 0})
    except Exception as e:
        return (FAIL, f"{model}: chat failed ({type(e).__name__}: {str(e)[:160]})")
    calls = r.choices[0].message.tool_calls
    mode = "JSON tool protocol" if llm._local_json_tools else "native tool calls"
    if calls:
        return (OK, f"{model}: made a tool call via {mode} ({calls[0].function.name} {calls[0].function.arguments[:60]})")
    return (WARN, f"{model}: answered but made no tool call. Try PAPERCI_LOCAL_TOOLS=json, or a stronger "
                  "instruction-tuned model (Qwen2.5-Coder 7B+, Llama 3.1 8B+).")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="paperci.localrun")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="preflight: Docker, sandbox images, LLM endpoint and tool calling")
    c.add_argument("--build", action="store_true", help="build missing sandbox images")
    c.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    rows = check_docker(a.build) + check_llm()
    print(f"models: {json.dumps(MODELS)}")
    if a.json:
        print(json.dumps([{"status": s.strip(), "detail": d} for s, d in rows], indent=1))
    else:
        for s, d in rows:
            print(f"[{s}] {d}")
    bad = sum(1 for s, _ in rows if s == FAIL)
    print("ready: start the agent with  python -m paperci.web  then open http://127.0.0.1:8000" if not bad
          else f"{bad} problem(s) above; fix them and run the check again.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
