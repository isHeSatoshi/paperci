"""Funnel, cost and failure statistics computed from the state DB (measured only)."""
from __future__ import annotations

import json
from collections import Counter

from . import db


def _stage_rows(stage: str, label: str | None = None) -> list[dict]:
    sql, args = "SELECT * FROM stages WHERE stage=? AND status='done'", [stage]
    if label:
        sql += " AND run_label=?"; args.append(label)
    return [dict(r) | {"out": json.loads(r["output"] or "{}")} for r in db.q(sql, tuple(args))]


def label_stats(label: str) -> dict:
    builds = _stage_rows("build", label)
    verdicts = _stage_rows("verdict", label)
    pk = [r for r in _stage_rows("package", label) if r["out"].get("task_id")]
    orc = _stage_rows("oracle", label)
    tr = _stage_rows("trainability", label)
    spend = db.q1("SELECT COALESCE(SUM(cost_usd),0) c FROM spend WHERE run_label=? OR run_label LIKE ?", (label, label + "-solver%"))["c"]
    build_spend = db.q1("SELECT COALESCE(SUM(cost_usd),0) c FROM spend WHERE run_label=? AND stage='builder'", (label,))["c"]
    fails = db.q("SELECT category, recovered FROM failures WHERE run_label=? AND stage LIKE 'builder.%'", (label,))
    n_built = sum(1 for b in builds if b["out"].get("status") == "built")
    vc = Counter(v["out"]["verdict"] for v in verdicts)
    measured = sum(1 for v in verdicts if any(pc.get("comparison") for pc in v["out"].get("per_claim", {}).values()))
    validated = sum(1 for o in orc if o["out"].get("reward", 0) >= 0.999)
    return {
        "label": label, "papers_attempted": len(builds), "built": n_built,
        "build_success_rate": round(n_built / len(builds), 3) if builds else None,
        "metric_measured": measured, "verdicts": dict(vc), "packaged": len(pk), "oracle_validated": validated,
        "trainability": dict(Counter(t["out"]["label"] for t in tr)),
        "avg_builder_turns": round(sum(b["out"].get("turns", 0) for b in builds) / len(builds), 2) if builds else None,
        "avg_build_minutes": round(sum(b["out"].get("seconds", 0) for b in builds) / len(builds) / 60, 2) if builds else None,
        "llm_cost_usd": round(spend, 4), "builder_cost_usd": round(build_spend, 4),
        "cost_per_attempt": round(spend / len(builds), 4) if builds else None,
        "cost_per_validated_task": round(spend / validated, 4) if validated else None,
        "failures": dict(Counter(f["category"] for f in fails)),
        "failures_recovered": sum(f["recovered"] for f in fails), "failures_total": len(fails),
        "give_up": dict(Counter(b["out"].get("give_up_category") for b in builds if b["out"].get("status") == "gave_up")),
        "build_status": dict(Counter(b["out"].get("status") for b in builds)),
    }


def funnel() -> dict:
    papers = db.q1("SELECT COUNT(*) n FROM papers")["n"]
    split = {r["split"]: r["n"] for r in db.q("SELECT split, COUNT(*) n FROM papers GROUP BY split")}
    tri = _stage_rows("triage", "shared")
    acc = [t for t in tri if t["out"].get("decision") == "accept"]
    cl = _stage_rows("claims", "shared")
    with_claims = [c for c in cl if c["out"].get("n_claims", 0) > 0]
    labels = [r["run_label"] for r in db.q("SELECT DISTINCT run_label FROM stages WHERE stage='build'")]
    reject_reasons = Counter()
    for t in tri:
        if t["out"].get("decision") == "reject":
            rr = t["out"].get("rule_reasons") or ["llm: not CPU-feasible"]
            for r in rr:
                reject_reasons[r.split(":")[0].split("(")[0].strip()] += 1
    front_cost = db.q1("SELECT COALESCE(SUM(cost_usd),0) c FROM spend WHERE run_label='shared'")["c"]
    return {"watched_with_code": papers, "split": split, "triaged": len(tri), "triage_accept": len(acc),
            "reject_reasons": dict(reject_reasons.most_common(8)), "claims_extracted": len(with_claims),
            "claims_total": sum(c["out"]["n_claims"] for c in with_claims),
            "front_half_cost_usd": round(front_cost, 4),
            "runs": {l: label_stats(l) for l in labels},
            "total_llm_spend_usd": round(db.q1("SELECT COALESCE(SUM(cost_usd),0) c FROM spend")["c"], 4)}
