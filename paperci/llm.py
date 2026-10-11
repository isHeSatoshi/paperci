"""OpenAI-compatible client (LiteLLM proxy) wrapped by neatlogs, with spend
accounting, bounded retries, and typed (Pydantic) JSON outputs."""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Type, TypeVar

import neatlogs
import openai
from openai import OpenAI
from pydantic import BaseModel, ValidationError

from . import ledger, tracing
from .config import FORBIDDEN_MODELS, LITELLM_BASE_URL, LLM_MAX_RETRIES, PRICES

T = TypeVar("T", bound=BaseModel)
_client: OpenAI | None = None


def client() -> OpenAI:
    global _client
    if _client is None:
        _client = neatlogs.wrap(OpenAI(base_url=LITELLM_BASE_URL, api_key=os.environ["LITELLM_API_KEY"],
                                       max_retries=0, timeout=180))
    return _client


def _chars(messages: list[dict]) -> int:
    n = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            n += len(c)
        elif isinstance(c, list):
            n += sum(len(b.get("text", "")) for b in c if isinstance(b, dict))
        if m.get("tool_calls"):
            n += len(json.dumps(m["tool_calls"]))
    return n


def estimate(model: str, messages: list[dict], max_tokens: int, tools: list | None = None) -> float:
    p = PRICES.get(model) or PRICES["claude-sonnet-4.6"]
    tin = (_chars(messages) + (len(json.dumps(tools)) if tools else 0)) / 3.2
    return tin * p["in"] + min(max_tokens, 2000) * p["out"]


def _with_cache_marks(messages: list[dict]) -> list[dict]:
    """Anthropic prompt caching: mark the system prompt and the newest message."""
    out = [dict(m) for m in messages]
    idxs = [0] if out and out[0]["role"] == "system" else []
    if len(out) > 1 and out[-1]["role"] in ("user", "tool"):
        idxs.append(len(out) - 1)
    for i in idxs:
        c = out[i].get("content")
        if isinstance(c, str) and c:
            out[i]["content"] = [{"type": "text", "text": c, "cache_control": {"type": "ephemeral"}}]
    return out


def chat(*, arxiv_id: str, stage: str, run_label: str, model: str, messages: list[dict],
         tools: list | None = None, max_tokens: int = 1500, temperature: float = 0.0,
         stage_cap: float | None = None, cache: bool = False, allow_reserve: bool = False) -> Any:
    if model in FORBIDDEN_MODELS:
        raise ValueError(f"model {model} is disabled by budget policy")
    est = estimate(model, messages, max_tokens, tools)
    ledger.precheck(arxiv_id, run_label, est, stage_cap=stage_cap, stage=stage, allow_reserve=allow_reserve)
    send = _with_cache_marks(messages) if cache and model.startswith("claude") else messages
    kwargs: dict[str, Any] = dict(model=model, messages=send, max_tokens=max_tokens, temperature=temperature)
    if tools:
        kwargs["tools"] = tools
    last_err: Exception | None = None
    for attempt in range(LLM_MAX_RETRIES + 1):
        try:
            if model.startswith(FACTORY_PREFIX):
                resp = _factory_chat(model, messages, tools, stage)
            else:
                resp = client().chat.completions.create(**kwargs)
            break
        except (openai.RateLimitError, openai.APIConnectionError, openai.APITimeoutError,
                openai.InternalServerError, FactoryError) as e:
            last_err = e
            tracing.log("llm retry {attempt} after {err}", level="warning", attempt=attempt,
                        err=type(e).__name__, model=model, stage=stage)
            time.sleep(min(30, 3 * 2 ** attempt))
    else:
        raise RuntimeError(f"LLM call failed after retries: {type(last_err).__name__}")
    u = resp.usage
    pt = int(getattr(u, "prompt_tokens", 0) or 0)
    ct = int(getattr(u, "completion_tokens", 0) or 0)
    cr = int(getattr(u, "cache_read_input_tokens", 0) or 0)
    cw = int(getattr(u, "cache_creation_input_tokens", 0) or 0)
    if not cr:
        det = getattr(u, "prompt_tokens_details", None)
        cr = int(getattr(det, "cached_tokens", 0) or 0) if det else 0
    cost = ledger.record(arxiv_id, stage, run_label, model, pt, ct, cr, cw)
    tracing.log("llm cost {stage} {model} ${cost:.5f}", stage=stage, model=model, cost=cost,
                prompt_tokens=pt, completion_tokens=ct, cache_read=cr, cache_write=cw)
    return resp


# ---------------------------------------------------------------- Factory (Droid) backend
# A Factory API key has no chat-completions endpoint, so a call becomes one locked-down `droid exec` run:
# a fresh empty working directory, only the TodoWrite tool (no shell, file, web or MCP access on the host),
# and tool calls returned as JSON that is mapped back to the OpenAI response shape the agents already use.
FACTORY_PREFIX = "factory:"
FACTORY_TIMEOUT = int(os.getenv("PAPERCI_FACTORY_TIMEOUT", "420"))


class FactoryError(Exception):
    pass


def _flatten(messages: list[dict], tools: list | None) -> str:
    def text(c: Any) -> str:
        if isinstance(c, list):
            return "".join(b.get("text", "") for b in c if isinstance(b, dict))
        return c or ""
    parts = ["You are used as a plain language model inside an automated pipeline. Do not call any of your own "
             "tools. Follow the SYSTEM instructions and answer the final turn of the conversation.\n"]
    for m in messages:
        role = m["role"]
        if role == "system":
            parts.append(f"=== SYSTEM ===\n{text(m.get('content'))}\n")
        elif role == "tool":
            parts.append(f"=== TOOL RESULT ({m.get('tool_call_id')}) ===\n{text(m.get('content'))}\n")
        else:
            body = text(m.get("content"))
            for tc in m.get("tool_calls") or []:
                f = tc["function"]
                body += f"\n[called {f['name']} id={tc['id']} with {f['arguments']}]"
            parts.append(f"=== {role.upper()} ===\n{body}\n")
    if tools:
        specs = [{"name": t["function"]["name"], "description": t["function"].get("description", ""),
                  "parameters": t["function"].get("parameters", {})} for t in tools]
        parts.append("=== YOUR TURN ===\nFunctions you can call (JSON schema):\n" + json.dumps(specs) +
                     '\nReply with ONLY one JSON object, no prose outside it: {"content": "<one short sentence of '
                     'reasoning>", "tool_calls": [{"name": "<function name>", "arguments": {...}}]}. Include at '
                     "least one tool call; several independent calls may go in one reply.")
    else:
        parts.append("=== YOUR TURN ===\nReply as the assistant.")
    return "\n".join(parts)


def _droid() -> str:
    import shutil
    exe = os.getenv("PAPERCI_DROID_BIN") or shutil.which("droid") or shutil.which("droid.cmd")
    if not exe:
        raise FactoryError("droid CLI not found")
    return exe


def _factory_chat(model: str, messages: list[dict], tools: list | None, stage: str) -> Any:
    import subprocess
    import tempfile
    import uuid
    from types import SimpleNamespace as NS
    if not os.environ.get("FACTORY_API_KEY"):
        raise ValueError("FACTORY_API_KEY is not set")
    prompt = _flatten(messages, tools)
    with tempfile.TemporaryDirectory(prefix="pci-llm-") as cwd:
        pf = os.path.join(cwd, "prompt.txt")
        with open(pf, "w", encoding="utf-8") as f:
            f.write(prompt)
        cmd = [_droid(), "exec", "-m", model[len(FACTORY_PREFIX):], "-r", os.getenv("PAPERCI_FACTORY_EFFORT", "low"),
               "--output-format", "json", "-f", pf, "--cwd", cwd, "--disable-builtin-skills", "--restrict-tools", "TodoWrite"]
        t0 = time.time()
        with tracing.span(f"llm:{stage}", kind="LLM", model=model, prompt_chars=len(prompt)) as s:
            try:
                p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                   timeout=FACTORY_TIMEOUT, cwd=cwd, env=os.environ)
            except subprocess.TimeoutExpired as e:
                raise FactoryError(f"droid exec timed out after {FACTORY_TIMEOUT}s") from e
            line = next((ln for ln in reversed(p.stdout.splitlines()) if ln.startswith("{")), "")
            try:
                out = json.loads(line)
            except json.JSONDecodeError:
                raise FactoryError(f"droid exec rc={p.returncode}: {(p.stderr or p.stdout)[-300:]}")
            if out.get("is_error") or p.returncode != 0:
                raise FactoryError(f"droid exec failed: {str(out.get('result') or p.stderr)[-300:]}")
            u = out.get("usage") or {}
            result = out.get("result") or ""
            tracing.set_attr(s, "seconds", round(time.time() - t0, 1))
            tracing.set_attr(s, "factory_credits", u.get("factory_credits"))
            tracing.set_attr(s, "input_tokens", u.get("input_tokens"))
            tracing.set_attr(s, "output_tokens", u.get("output_tokens"))
            tracing.set_attr(s, "response", result[:3000])
    content, calls = result, None
    if tools:
        try:
            j = extract_json(result)
            content = str(j.get("content") or "")
            calls = [NS(id=f"call_{uuid.uuid4().hex[:10]}", type="function",
                        function=NS(name=str(c["name"]), arguments=json.dumps(c.get("arguments") or {})))
                     for c in (j.get("tool_calls") or []) if isinstance(c, dict) and c.get("name")] or None
        except Exception:
            content, calls = result, None  # the agent loop asks the model to call a tool
    usage = NS(prompt_tokens=int(u.get("input_tokens") or 0), completion_tokens=int(u.get("output_tokens") or 0),
               cache_read_input_tokens=int(u.get("cache_read_input_tokens") or 0),
               cache_creation_input_tokens=int(u.get("cache_creation_input_tokens") or 0))
    return NS(choices=[NS(message=NS(content=content, tool_calls=calls))], usage=usage)


_JSON_RE = re.compile(r"\{.*\}|\[.*\]", re.S)


def extract_json(text: str) -> Any:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = _JSON_RE.search(text)
        if not m:
            raise
        return json.loads(m.group(0))


def chat_typed(schema: Type[T], *, system: str, user: str, max_tokens: int = 1500,
               repair_attempts: int = 1, **kw: Any) -> T:
    """Ask for JSON matching `schema`; on parse/validation error, send the error back once."""
    shape = json.dumps(schema.model_json_schema())[:3000]
    messages = [
        {"role": "system", "content": system + "\n\nReply with ONLY a JSON object matching this JSON schema:\n" + shape},
        {"role": "user", "content": user},
    ]
    for attempt in range(repair_attempts + 1):
        resp = chat(messages=messages, max_tokens=max_tokens, **kw)
        text = resp.choices[0].message.content or ""
        try:
            return schema.model_validate(extract_json(text))
        except (ValidationError, json.JSONDecodeError, ValueError) as e:
            tracing.log("typed output invalid (attempt {attempt}): {err}", level="warning",
                        attempt=attempt, err=str(e)[:300])
            if attempt >= repair_attempts:
                raise
            messages += [{"role": "assistant", "content": text},
                         {"role": "user", "content": f"That was not valid. Error: {str(e)[:500]}. Reply with only the corrected JSON."}]
    raise RuntimeError("unreachable")
