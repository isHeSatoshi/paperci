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

from . import llm, tracing
from .config import DATA_DIR, MODELS, ROOT, RUNS_DIR
from .recovery import classify, load_playbook, save_playbook, snippet_for
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


HARNESS_CMD = re.compile(r"^(cat /work/paperci_run\.sh 2>/dev/null|diff -ruN |test -s /work/paperci_run\.sh|du -sm |curl -fsSL --retry 3)")
_LOG_HDR = re.compile(r"^\$ (.*?)\n# exit=(-?\d+) seconds=([\d.]+)\n", re.S)


def _parse_cmd_log(p) -> tuple[str, int, str] | None:
    t = p.read_text(encoding="utf-8", errors="replace")
    m = _LOG_HDR.match(t)
    return (m.group(1), int(m.group(2)), t[m.end():]) if m else None


def _evidence(labels: list[str]) -> dict[str, list[dict]]:
    """Rebuild the failure taxonomy from raw sandbox logs and build results, using the
    current classifier (so classifier fixes apply retroactively to old traces)."""
    clusters: dict[str, list[dict]] = defaultdict(list)
    for label in labels:
        for bdir in RUNS_DIR.glob(f"*/{label}/build"):
            paper = bdir.parent.parent.name
            res_f = bdir / "result.json"
            if not res_f.exists():
                continue
            res = json.loads(res_f.read_text(encoding="utf-8"))
            built = res.get("status") == "built"
            logs = sorted((bdir / "cmds").glob("cmd_*.log"))
            parsed = [x for x in (_parse_cmd_log(p) for p in logs) if x]
            for i, (cmd, code, out) in enumerate(parsed):
                if i == 0 or HARNESS_CMD.match(cmd):  # harness-internal commands are not agent behaviour
                    continue
                cat = classify(out, code, cmd)
                if not cat:
                    continue
                nxt = " || ".join(c[:160] for c, _, _ in parsed[i + 1:i + 3])
                clusters[cat].append({"paper": paper, "detail": f"$ {cmd[:200]}\n" + snippet_for(out, cat)[-600:],
                                      "next_actions": nxt, "recovered": built})
            for host, n in (res.get("egress") or {}).get("denied", {}).items():
                clusters["egress_denied"].append({"paper": paper, "detail": f"proxy refused host {host} {n} times",
                                                  "next_actions": "", "recovered": built})
            if res.get("status") in ("max_turns", "wall_clock"):
                reads = sum(1 for c, _, _ in parsed if re.match(r"\s*(ls|cat|head|grep|find|sed -n|python3? -c)", c))
                clusters["turn_budget_exhausted"].append({
                    "paper": paper, "recovered": False, "next_actions": "",
                    "detail": f"status={res['status']} turns={res.get('turns')} read_like_cmds={reads}/{len(parsed)} "
                              f"never submitted; first commands: " + " | ".join(c[:80] for c, _, _ in parsed[1:6])})
            if res.get("status") == "gave_up" and (res.get("turns") or 0) > 6:
                clusters["late_give_up"].append({
                    "paper": paper, "recovered": False, "next_actions": "",
                    "detail": f"gave_up category={res.get('give_up_category')} at turn {res.get('turns')} "
                              f"(cost ${res.get('cost_usd')}): {res.get('reason', '')[:400]}"})
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
    allow = (ROOT / "sandbox" / "proxy" / "allowlist").read_text().split()
    for host in re.findall(r"https?://([^/\s'\"`]+)", rule.advice):
        if not any(re.match(p, host) for p in allow):
            return False, f"advice references non-allowlisted host {host}"
    if rule.category == "egress_denied":
        # Knowledge can go stale: a host denied in old traces may be allowlisted since.
        for host in re.findall(r"([a-z0-9][a-z0-9.\-]+\.(?:org|com|io|co|net|edu|nl))", rule.pattern + " " + rule.advice):
            if any(re.match(p, host) or re.match(p, "www." + host) or re.match(p, "api." + host) for p in allow):
                return False, f"stale: {host} is allowlisted now"
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
        allow = (ROOT / "sandbox" / "proxy" / "allowlist").read_text().split()
        user = (f"CATEGORIES: {sorted(clusters)}\n\nCURRENT EGRESS ALLOWLIST (regexes; hosts here are reachable NOW, "
                f"even if old traces show them refused):\n{allow}\n\nEXISTING RULES:\n{existing}\n\nFAILURE CLUSTERS:\n" + "\n".join(summary)
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
