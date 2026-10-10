"""Read a PaperCI trace back from neatlogs via the CLI (OAuth profile; the SDK key is
removed from the child environment as the setup guide requires).

  python scripts/nl_trace.py list [N]
  python scripts/nl_trace.py spans <trace-id> [--raw]
  python scripts/nl_trace.py summary <trace-id>     # compact JSON for docs/evidence.md
"""
import json
import os
import shutil
import subprocess
import sys
from collections import Counter

BASE = ["--host", "https://app.neatlogs.com", "--profile", "setup-https---app-neatlogs-com",
        "--project", "da3f6fba-4dfc-47d7-9960-3e3caedf7360"]
EXE = shutil.which("neatlogs") or shutil.which("neatlogs.cmd")


def cli(*args: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("NEATLOGS_API_KEY", "NEATLOGS_TOKEN", "NEATLOGS_PROJECT_ID")}
    p = subprocess.run([EXE, *BASE, *args], capture_output=True, text=True, env=env, timeout=120, encoding="utf-8")
    out = p.stdout.strip()
    return json.loads(out[out.index("{"):]) if "{" in out else {"error": p.stderr[-500:]}


def spans(tid: str) -> list[dict]:
    d = cli("traces", "spans", "list", tid, "--all", "--max-items", "1000", "--limit", "50")
    data = d.get("data", {})
    return data.get("spans") or data.get("items") or (data if isinstance(data, list) else [])


def attr(s: dict, key: str):
    a = s.get("attributes") or {}
    return a.get(key)


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "list":
        n = sys.argv[2] if len(sys.argv) > 2 else "10"
        for t in cli("traces", "list", "--limit", n).get("data", {}).get("traces", []):
            print(t["traceId"], t["traceName"], t["finalizationStatus"], t["durationMs"], "ms",
                  "llm", t["llmCallsCount"], "tools", t["toolCallsCount"], "tok", t["totalTokens"], t["tags"])
    elif cmd == "spans":
        ss = spans(sys.argv[2])
        if "--raw" in sys.argv:
            print(json.dumps(ss[:3], indent=1)[:6000])
        for s in ss:
            print(s.get("spanType"), "|", s.get("spanName"), "|", s.get("durationMs"), "ms |", s.get("status"),
                  "| parent", (s.get("parentSpanId") or "-")[:8])
    elif cmd == "summary":
        tid = sys.argv[2]
        t = cli("traces", "get", tid).get("data", {})
        ss = spans(tid)
        kinds = Counter((s.get("spanType") or "?") for s in ss)
        names = Counter(s.get("spanName") for s in ss)
        print(json.dumps({"trace_id": tid, "name": t.get("traceName"), "status": t.get("status"),
                          "finalization": t.get("finalizationStatus"), "duration_s": round((t.get("durationMs") or 0) / 1000, 1),
                          "llm_calls": t.get("llmCallsCount"), "tool_calls": t.get("toolCallsCount"),
                          "tokens": t.get("totalTokens"), "tags": t.get("tags"), "span_kinds": dict(kinds),
                          "top_spans": names.most_common(8)}, indent=1))
