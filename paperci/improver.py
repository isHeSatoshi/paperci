"""Self-improvement: mine traced build failures -> propose playbook rules ->
gate them deterministically -> adopt as a new playbook generation.

Boundary: the improver can only change the builder's guidance (data in
data/playbook.json). It cannot touch the verifier, claim sealing, tolerances,
or the held-out split, so it cannot make results look better by moving the judge."""
from __future__ import annotations

import json
import re
import time
from collections import defaultdict

from . import db, llm, tracing
from .config import DATA_DIR, MODELS, ROOT, RUNS_DIR
from .recovery import load_playbook, save_playbook
from .schemas import ImproverProposal, PlaybookRule

BANNED = re.compile(r"hard-?cod|fake|fabricat|skip (the )?verif|copy .*result|precomputed|print (the )?expected|sealed|guess", re.I)
HISTORY = DATA_DIR / "playbook_history"

SYSTEM = """You improve an autonomous research-code reproduction agent ("builder") that works in a CPU-only,
no-GPU Docker sandbox with an allowlist proxy (PyPI, GitHub, PyTorch wheels, Hugging Face, Zenodo, UCI)
during setup and NO network during the final run.
You get clusters of real failures observed in its traces, with the error snippet and what the builder did
next (and whether the build eventually succeeded). Write short, general, reusable recovery rules.
Each rule: rule_id (snake_case), category (one of the given categories), pattern (a Python regex that
matches the error text of that failure class), advice (<= 300 chars, concrete commands or tactics that
generalize to other repositories). Never advise hardcoding numbers, faking outputs, copying result files,
or skipping verification. Prefer advice that saves turns."""


def _evidence(labels: list[str]) -> dict[str, list[dict]]:
    marks = ",".join("?" * len(labels))
    rows = db.q(f"SELECT * FROM failures WHERE run_label IN ({marks}) AND stage LIKE 'builder.%' ORDER BY id", tuple(labels))
    clusters: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        r = dict(r)
        nxt = ""
        actions_f = RUNS_DIR / r["arxiv_id"].replace("/", "_") / r["run_label"] / "build" / "actions.json"
        if actions_f.exists():
            acts = json.loads(actions_f.read_text(encoding="utf-8"))
            snippet_key = r["detail"][-80:]
            for i, a in enumerate(acts):
                if a["type"] == "shell" and a.get("exit", 0) != 0:
                    # first failing command whose output we can't see here; take the next two actions
                    nxt = " || ".join((x.get("cmd") or f"write {x.get('path')}")[:160] for x in acts[i + 1:i + 3])
                    acts[i]["exit"] = 0  # consume, so the next failure maps to the next failing action
                    break
            actions_f_cache = acts  # noqa: F841
        clusters[r["category"]].append({"paper": r["arxiv_id"], "detail": r["detail"][-700:],
                                        "next_actions": nxt, "recovered": bool(r["recovered"])})
    return clusters


def _gate(rule: PlaybookRule, clusters: dict[str, list[dict]]) -> tuple[bool, str]:
    if len(rule.pattern) > 200 or len(rule.advice) > 400:
        return False, "too long"
    try:
        rx = re.compile(rule.pattern, re.I)
    except re.error as e:
        return False, f"bad regex: {e}"
    if BANNED.search(rule.advice):
        return False, "advice violates integrity policy"
    for host in re.findall(r"https?://([^/\s]+)", rule.advice):
        allow = (ROOT / "sandbox" / "proxy" / "allowlist").read_text()
        if not any(re.match(p, host) for p in allow.split()):
            return False, f"advice references non-allowlisted host {host}"
    own = clusters.get(rule.category, [])
    others = [x for c, xs in clusters.items() if c != rule.category for x in xs]
    hit_own = sum(1 for x in own if rx.search(x["detail"]))
    hit_other = sum(1 for x in others if rx.search(x["detail"]))
    if own and hit_own == 0:
        return False, "pattern matches none of its own failures"
    if others and hit_other / len(others) > 0.25:
        return False, f"pattern too broad ({hit_other}/{len(others)} other-class failures)"
    rule.source_failures = len(own)
    return True, f"recall {hit_own}/{len(own)}, false hits {hit_other}/{len(others)}"


def improve(labels: list[str], apply: bool = False) -> dict:
    clusters = _evidence(labels)
    with tracing.span("stage:improver", kind="AGENT", labels=labels, model=MODELS["builder"]) as s:
        if not clusters:
            return {"status": "no failures to learn from"}
        pb = load_playbook()
        summary = []
        for cat, xs in sorted(clusters.items(), key=lambda kv: -len(kv[1])):
            summary.append(f"## {cat}: {len(xs)} occurrences, {sum(x['recovered'] for x in xs)} in builds that succeeded")
            for x in xs[:4]:
                summary.append(f"- error: {x['detail'][-450:]!r}\n  next: {x['next_actions']!r}")
        existing = "\n".join(f"- {r.rule_id} ({r.category}): {r.advice}" for r in pb.rules) or "(none)"
        user = (f"CATEGORIES: {sorted(clusters)}\n\nEXISTING RULES:\n{existing}\n\nFAILURE CLUSTERS:\n" + "\n".join(summary)
                + "\n\nPropose up to 8 new or revised rules (reuse a rule_id to revise it).")
        prop = llm.chat_typed(ImproverProposal, system=SYSTEM, user=user[:24000], max_tokens=2000,
                              arxiv_id="-", stage="improver", run_label="improver", model=MODELS["builder"])
        accepted, rejected = [], []
        for r in prop.rules:
            ok, why = _gate(r, clusters)
            (accepted if ok else rejected).append({"rule": r.model_dump(), "gate": why})
        tracing.set_attr(s, "proposed", len(prop.rules))
        tracing.set_attr(s, "accepted", len(accepted))
        result = {"generation_before": pb.generation, "clusters": {k: len(v) for k, v in clusters.items()},
                  "accepted": accepted, "rejected": rejected, "rationale": prop.rationale}
        if apply and accepted:
            by_id = {r.rule_id: r for r in pb.rules}
            for a in accepted:
                by_id[a["rule"]["rule_id"]] = PlaybookRule.model_validate(a["rule"])
            pb.rules = list(by_id.values())
            pb.generation += 1
            pb.notes = f"gen {pb.generation} learned from {labels} at {time.strftime('%Y-%m-%d %H:%M')}"
            save_playbook(pb)
            HISTORY.mkdir(exist_ok=True)
            (HISTORY / f"gen{pb.generation}.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
            result["generation_after"] = pb.generation
        return result
