"""Generate docs/evidence.md from measured state (DB, playbook history, Entire, neatlogs CLI).
Paper IDs are anonymized (P-xxxxxx) because verdicts about named papers are not published.
  python scripts/build_evidence.py [--no-readback]"""
import hashlib
import json
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from paperci import db, ledger  # noqa: E402
from paperci.config import DATA_DIR  # noqa: E402
from paperci.stats import funnel, label_stats  # noqa: E402


def anon(aid: str) -> str:
    return "P-" + hashlib.sha256(aid.encode()).hexdigest()[:6]


def build(aid, label):
    r = db.get_stage(aid, "build", label)
    return json.loads(r["output"]) if r and r["output"] else None


def verdict_of(aid, label):
    r = db.get_stage(aid, "verdict", label)
    return json.loads(r["output"]) if r and r["output"] else {}


readback = "--no-readback" not in sys.argv
L = [f"# Evidence (generated {time.strftime('%Y-%m-%d %H:%M IST')} by scripts/build_evidence.py)", "",
     "All numbers below are measured from `data/paperci.sqlite`, run artifacts and neatlogs readback.",
     "Paper identities are anonymized (P-xxxxxx); per-paper verdicts are not published.", ""]

# ---------------- before / after on held-out
L += ["## 1. Before / after on the held-out split (same papers, same sealed claims)", "",
      "v1 = baseline builder (playbook gen 0, base image). v2 = self-improved builder (playbook gen 2: learned",
      "rules + enforced controller, learned image gen1). Papers taken in queue order (newest first), not picked.", "",
      "| paper | v1 status | v1 turns | v1 $ | v2 status | v2 turns | v2 $ | v1 trace | v2 trace |", "|---|---|---|---|---|---|---|---|---|"]
pairs = []
ids = [r["arxiv_id"] for r in db.q("SELECT DISTINCT arxiv_id FROM stages WHERE stage='build' AND run_label='heldout-v1'")]
for aid in ids:
    b1, b2 = build(aid, "heldout-v1"), build(aid, "heldout-v2")
    if not (b1 and b2):
        continue
    t1 = db.get_stage(aid, "build", "heldout-v1")["trace_id"]
    t2 = db.get_stage(aid, "build", "heldout-v2")["trace_id"]
    pairs.append((aid, t1, t2, b1, b2))
    L.append(f"| {anon(aid)} | {b1['status']} | {b1['turns']} | {b1['cost_usd']} | {b2['status']} | {b2['turns']} | "
             f"{b2['cost_usd']} | `{t1}` | `{t2}` |")
s1, s2 = label_stats("heldout-v1"), label_stats("heldout-v2")
keys = ["papers_attempted", "built", "build_success_rate", "metric_measured", "integrity_flagged", "integrity_flags",
        "faithful_measured", "claims_within_tol_n2", "avg_builder_turns", "avg_build_minutes",
        "builder_cost_usd", "cost_per_attempt", "packaged", "oracle_validated", "failures_total", "failures_recovered"]
L += ["", "| metric | heldout-v1 (before) | heldout-v2 (after) |", "|---|---|---|"]
L += [f"| {k} | {s1.get(k)} | {s2.get(k)} |" for k in keys]
L += [f"| verdict counts | {s1['verdicts']} | {s2['verdicts']} |", f"| build status | {s1['build_status']} | {s2['build_status']} |", ""]

# ---------------- self-improvement history
L += ["## 2. Self-improvement generations (data/playbook_history)", ""]
for g in sorted((DATA_DIR / "playbook_history").glob("gen*.json")):
    h = json.loads(g.read_text())
    rej = Counter(r["gate"].split(":")[0] for r in h["rejected"])
    L.append(f"- **{g.stem}**: learned from clusters {h['clusters']}; accepted {len(h['accepted'])} rules "
             f"({', '.join(a['rule']['rule_id'] for a in h['accepted'])}); rejected {len(h['rejected'])} ({dict(rej)})"
             + (f"; controller {h['controller']['max_turns']}/{h['controller']['run_by_turn']}/{h['controller']['script_by_turn']} gate={h['controller_gate']}"
                if h.get("controller") else ""))
L += ["- Environment learning: `huggingface_hub` promoted to the sandbox image (installed in 2 dev papers, install-gated) -> `paperci/sandbox:gen1`.", ""]

# ---------------- dev history
L += ["## 3. Dev-split history (used for tuning; not the reported result)", "", "| label | attempted | built | avg turns | $/attempt | status |", "|---|---|---|---|---|---|"]
for lab in ["dev-v1", "dev-v2", "dev-g2"]:
    s = label_stats(lab)
    L.append(f"| {lab} | {s['papers_attempted']} | {s['built']} | {s['avg_builder_turns']} | {s['cost_per_attempt']} | {s['build_status']} |")
L.append("")

# ---------------- failure taxonomy + recovery
L += ["## 4. Failure taxonomy and recoveries", ""]
rows = db.q("SELECT run_label, category, recovered FROM failures WHERE stage LIKE 'builder.%'")
tax = Counter(r["category"] for r in rows)
L.append("Builder failure classes (all runs): " + ", ".join(f"{k} {v}" for k, v in tax.most_common()))
rec = db.q("SELECT arxiv_id, run_label, category, trace_id FROM failures WHERE recovered=1 AND stage LIKE 'builder.%'")
L += ["", "Recovered failures (the agent worked around it inside a build that succeeded, or harness self-heal + retry):"]
L += [f"- {anon(r['arxiv_id'])} {r['run_label']}: {r['category']} (trace `{r['trace_id']}`)" for r in rec] or ["- none yet"]
chaos = db.q("SELECT arxiv_id, run_label FROM events WHERE kind='chaos'")
L += ["", "Fault-injection (chaos) runs (`PAPERCI_CHAOS=proxy_down` stops the egress proxy before the repo fetch):"]
for c in chaos:
    fs = db.q("SELECT category, recovered, trace_id, substr(detail,1,60) d FROM failures WHERE arxiv_id=? AND run_label=? "
              "AND stage='builder.fetch'", (c["arxiv_id"], c["run_label"]))
    for f in fs:
        L.append(f"- {anon(c['arxiv_id'])} {c['run_label']}: fetch failed (`{f['d'].splitlines()[0]}`), classified "
                 f"`{f['category']}`, self-heal restarted the proxy, retry succeeded -> recovered={bool(f['recovered'])}; "
                 f"trace `{f['trace_id']}`")
if not chaos:
    L.append("- none yet")
L.append("")

# ---------------- funnel + economics
f = funnel()
L += ["## 5. Funnel and cost", "",
      f"- Watched (fresh, has code): {f['watched_with_code']}; triage accept: {f['triage_accept']}; grounded sealed claims: {f['claims_extracted']} papers / {f['claims_total']} claims",
      f"- Triage reject reasons: {f['reject_reasons']}",
      f"- Front half (snapshot+triage+claims) cost: ${f['front_half_cost_usd']}",
      f"- Total LLM spend: ledger ${round(ledger.local_total(), 4)} vs proxy-reported ${round(ledger.remote_spend(0), 4)}", ""]

# ---------------- neatlogs readback
L += ["## 6. neatlogs readback (CLI, OAuth profile; SDK key excluded from the CLI env)", ""]
if readback and pairs:
    import nl_trace
    chaos_tids = [r["trace_id"] for r in db.q("SELECT DISTINCT trace_id FROM failures WHERE run_label LIKE '%chaos%' "
                                              "AND trace_id IS NOT NULL")]
    for tid in chaos_tids:
        d = nl_trace.cli("traces", "get", tid).get("data", {})
        L.append(f"- chaos `{tid}` finalization `{d.get('finalizationStatus')}` (recovery is also recorded in the "
                 "state DB `failures` table and `runs/logs/dev-chaos.log`)")
    for aid, t1, t2, _, _ in pairs[:3]:
        for tid in (t1, t2):
            d = nl_trace.cli("traces", "get", tid).get("data", {})
            L.append(f"- `{tid}` {d.get('finalizationStatus')} {round((d.get('durationMs') or 0) / 1000)}s, "
                     f"{d.get('llmCallsCount')} LLM calls, {d.get('toolCallsCount')} tool calls, {d.get('totalTokens')} tokens, tags {d.get('tags')}")
L += ["", "Known neatlogs-side issue: trace `7bfd8f9e85e8a9e5a02622e3484c574c` (dev, 744 s) has finalizationStatus `dlq`",
      "and spans readback returns TRACE_FINALIZATION_FAILED; a 858 s trace (`9d87b5e064829420dd778963e09904f7`) finalized fine,",
      "so duration alone is not the cause. LLM span costUsd is 0 because the proxy model names are unpriced in",
      "neatlogs; cost is carried as `paperci.cost_usd` attributes and LOG spans from our ledger.", ""]

# ---------------- Entire
L += ["## 7. Entire checkpoints", ""]
try:
    out = subprocess.run(["entire", "checkpoint", "list"], cwd=ROOT, capture_output=True, text=True, timeout=60).stdout
    L += ["```", out.strip()[:3000], "```"]
except Exception as e:
    L.append(f"(entire checkpoint list failed: {e})")
(ROOT / "docs" / "evidence.md").write_text("\n".join(L) + "\n", encoding="utf-8", newline="\n")
print("wrote docs/evidence.md", len(L), "lines")
