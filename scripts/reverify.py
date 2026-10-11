"""Re-run the (fixed, frozen) verifier on stored offline-run artifacts for every run label, so all
runs are judged by identical rules. No sandbox runs; LLM extraction only for ambiguous logs.
Packages whose verdict now carries integrity flags are invalidated.
    python scripts/reverify.py [label-prefix ...]     (default: every label)"""
import json
import shutil
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from paperci import db, tracing  # noqa: E402
from paperci.config import REGISTRY_DIR, RUNS_DIR  # noqa: E402
from paperci.pipeline import _apply_spec, _load  # noqa: E402
from paperci.stages import claims as claims_mod  # noqa: E402
from paperci.stages import tasks, verifier  # noqa: E402

tracing.init("reverify", "-")
try:
    only = sys.argv[1:]
    for row in db.q("SELECT arxiv_id, run_label FROM stages WHERE stage='robustness' AND status='done'"):
        aid, label = row["arxiv_id"], row["run_label"]
        if only and not any(label.startswith(p) for p in only):
            continue
        build = _load(aid, "build", label)
        rob = _load(aid, "robustness", label)
        snap = _load(aid, "snapshot", "shared")
        sealed = claims_mod.unseal(aid, "verifier")
        runs = rob["runs"]
        per_claim = {}
        if runs and runs[0]["exit_code"] == 0:
            arts0 = verifier.read_artifacts(runs[0]["artifact_dir"])
            mc = Counter(c.metric.strip().lower() for c in sealed)
            with tracing.span(f"reverify:{aid}", kind="WORKFLOW", run_label=label):
                for c in sealed:
                    ext = verifier.extract(aid, label, c, arts0, shared_metric=mc[c.metric.strip().lower()] > 1)
                    pc = {"metric": c.metric, "extraction": ext, "raw_values": [ext["value"]] if ext.get("found") else []}
                    if pc["raw_values"]:
                        spec = tasks.extraction_spec(c.claim_id, ext, pc["raw_values"][0])
                        for r in runs[1:]:
                            if r["exit_code"] == 0 and spec:
                                v = _apply_spec(spec, verifier.read_artifacts(r["artifact_dir"]))
                                if v is not None:
                                    pc["raw_values"].append(v)
                        vals = [verifier.normalize(c, v)[0] for v in pc["raw_values"]]
                        pc["normalization"] = verifier.normalize(c, pc["raw_values"][0])[1]
                        pc["comparison"] = verifier.compare(c, vals)
                    per_claim[c.claim_id] = pc
        integ = verifier.integrity(RUNS_DIR / aid.replace("/", "_") / label / "build", sealed, runs,
                                   build.get("egress", {}), tree=snap.get("tree", []))
        v, conf, reason = verifier.verdict(build, runs, per_claim, integ)
        old = _load(aid, "verdict", label) or {}
        db.save_stage(aid, "verdict", label, output={"verdict": v, "confidence": conf, "reason": reason,
                                                    "integrity": integ, "per_claim": per_claim, "reverified": True})
        db.set_paper(aid, verdict=v, confidence=conf)
        print(f"{aid} {label}: {old.get('verdict')}/{old.get('confidence')} -> {v}/{conf} flags={integ['flags']}")
        if integ["flags"]:
            pk = _load(aid, "package", label)
            if pk and pk.get("task_id"):
                tdir = REGISTRY_DIR / "tasks" / pk["task_id"]
                rej = REGISTRY_DIR / "rejected" / pk["task_id"]
                if tdir.exists():
                    rej.parent.mkdir(parents=True, exist_ok=True)
                    if rej.exists():
                        shutil.rmtree(rej)
                    shutil.move(str(tdir), str(rej))
                db.save_stage(aid, "package", label, output={"task_id": None, "invalidated": integ["flags"],
                                                            "was": pk["task_id"]})
                print("   package invalidated:", pk["task_id"])
finally:
    tracing.shutdown()
