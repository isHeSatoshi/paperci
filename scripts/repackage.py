"""Re-run only package -> oracle -> trainability for clean (unflagged, non-blocked) verdicts, after a
packager fix. Build/verify/robustness/verdict stages resume from state; no builder spend.
    python scripts/repackage.py [arxiv_id:label ...]"""
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from paperci import db, tracing  # noqa: E402
from paperci.config import REGISTRY_DIR  # noqa: E402
from paperci.pipeline import _load, reproduce  # noqa: E402

targets = [tuple(a.split(":", 1)) for a in sys.argv[1:]]
if not targets:
    for r in db.q("SELECT arxiv_id, run_label, output FROM stages WHERE stage='verdict' AND status='done'"):
        o = json.loads(r["output"])
        if o["verdict"] != "blocked-infra" and not o["integrity"]["flags"]:
            targets.append((r["arxiv_id"], r["run_label"]))
tracing.init("repackage", "-")
try:
    for aid, label in targets:
        pk = _load(aid, "package", label) or {}
        for name in {pk.get("task_id"), pk.get("was")} - {None}:
            for sub in ("tasks", "rejected"):
                d = REGISTRY_DIR / sub / name
                if d.exists():
                    shutil.rmtree(d)
        db.ex("DELETE FROM stages WHERE arxiv_id=? AND run_label=? AND stage IN ('package','oracle','trainability')",
              (aid, label))
        out = reproduce(aid, label, "v2")
        print(aid, label, json.dumps(out), flush=True)
        for st in ("package", "oracle", "trainability"):
            print("  ", st, json.dumps(_load(aid, st, label))[:300], flush=True)
finally:
    tracing.shutdown()
