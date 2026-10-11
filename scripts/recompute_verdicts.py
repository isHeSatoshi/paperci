"""Recompute every stored verdict with the current (frozen) verifier rules, so all run labels are
judged by identical logic. Does not re-run builds or LLM calls.  python scripts/recompute_verdicts.py"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from paperci import db  # noqa: E402
from paperci.config import RUNS_DIR  # noqa: E402
from paperci.stages import claims as claims_mod  # noqa: E402
from paperci.stages import verifier  # noqa: E402

changed = 0
for row in db.q("SELECT arxiv_id, run_label, output FROM stages WHERE stage='verdict' AND status='done'"):
    aid, label = row["arxiv_id"], row["run_label"]
    old = json.loads(row["output"])
    build = json.loads(db.get_stage(aid, "build", label)["output"])
    rob_row = db.get_stage(aid, "robustness", label)
    rob = json.loads(rob_row["output"]) if rob_row and rob_row["output"] else {"runs": [], "per_claim": {}}
    sealed = claims_mod.unseal(aid, "report")
    per_claim = rob["per_claim"]
    for c in sealed:
        pc = per_claim.get(c.claim_id)
        if pc and pc.get("raw_values"):
            vals = [verifier.normalize(c, v)[0] for v in pc["raw_values"]]
            pc["normalization"] = verifier.normalize(c, pc["raw_values"][0])[1]
            pc["comparison"] = verifier.compare(c, vals)
    integ = verifier.integrity(RUNS_DIR / aid.replace("/", "_") / label / "build", sealed, rob["runs"], build.get("egress", {}))
    v, conf, reason = verifier.verdict(build, rob["runs"], per_claim, integ)
    new = {"verdict": v, "confidence": conf, "reason": reason, "integrity": integ, "per_claim": per_claim}
    if (old["verdict"], old["confidence"]) != (v, conf):
        changed += 1
        print(f"{aid} {label}: {old['verdict']}/{old['confidence']} -> {v}/{conf}")
    db.save_stage(aid, "verdict", label, output=new)
print("verdicts recomputed; changed:", changed)
