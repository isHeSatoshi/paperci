"""Label the demo traces in neatlogs (tags + readable names + an explanatory comment) so they are easy to
find in the dashboard. Names carry no paper identity.   python scripts/nl_label_demo.py [--dry-run]"""
import json
import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from nl_trace import BASE, EXE, cli  # noqa: E402

DEMO = {
    "294209f0b2d1b60bfc1509fdbece2f1b": ("demo:before", "PaperCI BEFORE - held-out paper, builder v1 (gen 0)",
        "BEFORE. Held-out paper, baseline builder v1 (playbook gen 0). Built in 18 turns, $0.319, 19 LLM calls, "
        "274k tokens. Its packaged task FAILED its own oracle (reward 0) and was rejected."),
    "4e85d604ac7a5d343950ee923f573502": ("demo:after", "PaperCI AFTER - same paper, builder v2 (self-improved gen 2)",
        "AFTER. Same held-out paper and sealed claims, builder v2 (playbook gen 2: learned rules + enforced controller, "
        "image gen1). Built in 10 turns, $0.145, 13 LLM calls, 142k tokens. 2 claims within calibrated tolerance over "
        "3 offline reruns; task oracle-validated (reward 1.0)."),
    "14ed5fe7f083b2b1ff4c98fbbf705223": ("demo:chaos-recovery", "PaperCI CHAOS - proxy killed, self-heal, recovered",
        "FAILURE + RECOVERY. Fault injection PAPERCI_CHAOS=proxy_down stopped the egress proxy before the repo fetch. "
        "Fetch failed (curl: Could not resolve proxy), ensure_infra() restarted the proxy, retry succeeded, failure "
        "marked recovered. Builder then gave up correctly in 5 turns ($0.088): benchmark data not released."),
}


def run(args, body=None):
    env = {k: v for k, v in __import__("os").environ.items() if k not in ("NEATLOGS_API_KEY", "NEATLOGS_TOKEN", "NEATLOGS_PROJECT_ID")}
    p = subprocess.run([EXE, *BASE, "--json", *args], input=body, capture_output=True, text=True, env=env, timeout=120,
                       encoding="utf-8")
    return p.returncode, (p.stdout or p.stderr).strip()[-400:]


for tid, (tag, name, note) in DEMO.items():
    t = cli("traces", "get", tid).get("data", {})
    tags = [x for x in (t.get("tags") or []) if not x.startswith("demo:")] + [tag]
    patch = {"expectedRevision": t.get("metadataRevision", 0), "tags": tags[:10], "traceName": name}
    print(tid, json.dumps(patch))
    if "--dry-run" in sys.argv:
        continue
    print("  metadata:", run(["traces", "update-metadata", tid, "--body-stdin", "--idempotency-key", str(uuid.uuid4())],
                             json.dumps(patch)))
    existing = cli("comments", "list", "--trace", tid).get("data", {})
    if note[:40] in json.dumps(existing):
        print("  comment: already present")
        continue
    print("  comment:", run(["comments", "create", "--trace", tid, "--content-stdin", "--idempotency-key", str(uuid.uuid4())],
                            note))
