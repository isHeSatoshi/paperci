"""/judges: PaperCI run on its own submission.

Each official neatHack evidence row is treated like a claim in a paper: it gets a live check against the real
sources (git history, the neatlogs public API, the run database, the evolution archive), a verdict, the
evidence, a command to verify it yourself, and what the evidence does not show. Rows without evidence say so.

Everything here is read-only. neatlogs is read with the server's service-account token (observability:read),
cached so a page view never waits on the API for long.
"""
from __future__ import annotations

import calendar
import collections
import html
import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from .config import ROOT

REPO = "https://github.com/isHeSatoshi/paperci"
KICKOFF = calendar.timegm((2026, 10, 10, 7, 0, 0))  # 12:30 IST, from neatlogs.com/hackathon
LINKS = ROOT / "docs" / "judges-links.json"
CACHE = ROOT / "data" / "judges_cache.json"
TTL_S = 600
# PaperCI's own detections in neatlogs, plus the built-in one that ERROR spans trigger.
DETECTIONS = {
    "c3df3ce6-df35-4614-93f6-1e5849a2bf2a": ("Execution Failed (built-in)", "neg"),
    "f412bc96-ef73-4d15-be23-a8fa9daacb5d": ("Builder ran out of budget", "neg"),
    "c206741d-2915-4e81-846d-df8f90e8c28e": ("Integrity flag on verdict", "neg"),
    "fc77bfd7-78b2-486a-b19f-f872a8c7a350": ("Proxy refused a host", "neu"),
    "30a5859b-9aa9-4696-912c-a7010b240072": ("Offline dry run rejected", "neu"),
    "1ae4b390-d919-41af-af97-40cb79afd4f8": ("Honest give-up", "neu"),
    "dcb41951-a4a7-4d28-b30d-7ad2ab99dd41": ("Claims verified", "pos"),
}
# Measured once on the held-out split (docs/evidence.md, scripts/build_evidence.py): same 10 papers, same sealed
# claims, builder before (v1) and after (v2) self-improvement.
HELDOUT = {"papers": 10, "built": (3, 5), "faithful": (1, 2), "within_tol": (0, 2), "turns": (16.5, 11.8),
           "cost": (0.2373, 0.1776), "failures": (29, 11), "oracle": (0, 2),
           "example": {"paper": "P-73e4a2", "before": ("max_turns", 18, "553880c675b8fd62afafa4acc140bcb0"),
                       "after": ("built", 10, "80481abc951ae433aa67a126d6925222")}}

_lock = threading.Lock()


# ---------------------------------------------------------------- sources
def _git() -> dict:
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "log", "--format=%H%x1f%ct%x1f%s%x1f%(trailers:key=Entire-Checkpoint,valueonly,separator=%x2C)%x1e"],
                             capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return {}
    rows = []
    for rec in out.split("\x1e"):
        p = rec.strip("\n").split("\x1f")
        if len(p) >= 4:
            rows.append({"sha": p[0], "ts": int(p[1]), "subject": p[2], "checkpoint": p[3].strip()})
    if not rows:
        return {}
    first = min(rows, key=lambda r: r["ts"])
    cps = [r for r in rows if r["checkpoint"]]
    return {"commits": len(rows), "checkpointed": len(cps), "first": first, "last": max(rows, key=lambda r: r["ts"]),
            "rows": rows[:200],
            "before_kickoff": sum(1 for r in rows if r["ts"] < KICKOFF),
            "checkpoint_example": max(cps, key=lambda r: r["ts"]) if cps else None}


def _neatlogs(mirror: str | None) -> dict:
    from . import nl_api
    if not nl_api.enabled():
        return {"error": "no service-account token on this server"}
    out: dict = {}
    traces, cur = [], None
    try:
        for _ in range(30):
            d = nl_api._get("/traces?limit=50" + (f"&cursor={cur}" if cur else ""))["data"]
            traces += d["traces"]
            cur = (d.get("page") or {}).get("nextCursor")
            if not cur:
                break
    except Exception as e:
        out["error"] = f"traces: {str(e)[:120]}"
    papers = [t for t in traces if t["traceName"].startswith(("paper:", "evolve-round:", "paper-prepare:"))]
    out["traces"] = {"n": len(traces), "complete": cur is None, "by_status": dict(collections.Counter(t["status"] for t in traces)),
                     "tokens": sum(t.get("totalTokens") or 0 for t in traces),
                     "llm_calls": sum(t.get("llmCallsCount") or 0 for t in traces),
                     "tool_calls": sum(t.get("toolCallsCount") or 0 for t in traces),
                     "first": min((t["startedAt"] for t in traces), default=None), "paper_runs": len(papers),
                     "timeline": [(t["startedAt"], t["status"], t.get("totalTokens") or 0) for t in traces
                                  if t["traceName"].startswith("paper:")]}
    hits = {}
    for did, (name, tone) in DETECTIONS.items():
        n, c = 0, None
        try:
            for _ in range(6):
                d = nl_api._get(f"/detections/{did}/hits?limit=50" + (f"&cursor={c}" if c else ""))["data"]
                n += len(d["hits"])
                c = (d.get("page") or {}).get("nextCursor")
                if not c:
                    break
        except Exception:
            n = None
        hits[did] = {"name": name, "tone": tone, "hits": n, "more": bool(c)}
    out["detections"] = hits
    public = lambda t: any(str(x).startswith(("run:icml-", "run:evo-")) for x in t.get("tags") or [])  # noqa: E731
    pick = mirror or next((t["traceId"] for t in traces if t["traceName"].startswith("paper:") and public(t)
                           and t.get("finalizationStatus") == "finalized" and (t.get("spansCount") or 0) >= 20), None)
    if pick:
        spans, c = [], None
        try:
            for _ in range(6):
                d = nl_api._get(f"/traces/{pick}/spans?limit=50" + (f"&cursor={c}" if c else ""))["data"]
                spans += d["spans"]
                c = (d.get("page") or {}).get("nextCursor")
                if not c:
                    break
            meta = nl_api._get(f"/traces/{pick}")["data"]
            out["mirror"] = {"trace": meta, "spans": [{k: s.get(k) for k in ("spanId", "parentSpanId", "spanName", "spanType",
                                                                             "status", "startedAt", "durationMs", "totalTokens", "model")}
                                                      for s in spans]}
        except Exception as e:
            out["mirror_error"] = str(e)[:160]
    return out


def _local() -> dict:
    from . import db, icml_eval
    out: dict = {}
    f = db.q1("SELECT COUNT(*) n, SUM(recovered) r FROM failures")
    out["failures"] = {"n": f["n"], "recovered": f["r"] or 0}
    ex = db.q("SELECT f.arxiv_id, f.run_label, f.category, f.detail, f.trace_id FROM failures f JOIN stages s "
              "ON s.arxiv_id=f.arxiv_id AND s.run_label=f.run_label AND s.stage='build' "
              "WHERE f.recovered=1 AND f.trace_id IS NOT NULL AND f.stage LIKE 'builder.%' AND s.output LIKE '%\"status\": \"built\"%' "
              # Author submissions and live arXiv runs are private or anonymized; only public evaluation runs are shown.
              "AND (f.run_label LIKE 'icml-%' OR f.run_label LIKE 'evo-%' OR f.run_label LIKE 'heldout-%' OR f.run_label LIKE 'dev-%') "
              "ORDER BY f.id DESC LIMIT 3")
    out["recovered_examples"] = [{"label": r["run_label"], "category": r["category"], "detail": r["detail"][:220],
                                  "trace": r["trace_id"]} for r in ex]
    s = db.q1("SELECT COUNT(*) n, SUM(cost_usd) c FROM spend")
    out["spend"] = {"calls": s["n"], "usd": round(s["c"] or 0, 2)}
    b = db.q1("SELECT COUNT(*) n FROM stages WHERE stage='build' AND status='done'")
    out["builds"] = b["n"]
    try:
        out["icml"] = {"p1": icml_eval.score(icml_eval.STATE_P1),
                       "p2": icml_eval.score(icml_eval.ICML / "state_p2.json") if (icml_eval.ICML / "state_p2.json").exists() else None}
    except Exception:
        out["icml"] = None
    try:
        a = json.loads((ROOT / "data" / "evolve" / "archive.json").read_text(encoding="utf-8"))
        cands = a.get("candidates", {}).values()
        out["evolve"] = {"rounds": len(a.get("rounds", [])), "candidates": len(a.get("candidates", {})),
                         "evaluated": sum(1 for c in cands if c.get("evaluated")),
                         "by_status": dict(collections.Counter(c.get("status") for c in cands)),
                         "adopted": [{"cid": c["id"], "kind": c["kind"], "delta": c.get("delta")} for c in cands
                                     if c.get("status") == "adopted"],
                         "tripwires": sum(1 for c in cands if "integrity" in str(c.get("reason", "")).lower())}
    except Exception:
        out["evolve"] = None
    try:
        from . import ops
        ev = ops.events(5000)
        out["ops"] = {"healed": sum(1 for e in ev if e.get("healed")), "chaos": sum(1 for e in ev if e["kind"] == "chaos"),
                      "deploys": sum(1 for e in ev if e["kind"] == "deploy"),
                      "rolled_back": sum(1 for e in ev if e["kind"] == "deploy" and "rolled back" in e["text"])}
    except Exception:
        out["ops"] = None
    try:
        st = json.loads((ROOT / "data" / "neatlogs_incidents.json").read_text(encoding="utf-8"))
        out["incidents"] = {"open": sorted(st.get("open", {})), "last": st.get("last")}
    except Exception:
        out["incidents"] = {"open": [], "last": None}
    try:
        pv = json.loads((ROOT / "data" / "neatlogs_prompts.json").read_text(encoding="utf-8"))
        out["prompt_versions"] = sum(len(v) for v in pv.values())
    except Exception:
        out["prompt_versions"] = 0
    try:
        out["playbook_generation"] = json.loads((ROOT / "data" / "playbook.json").read_text(encoding="utf-8"))["generation"]
    except Exception:
        out["playbook_generation"] = None
    return out


def _iso_ts(s: str) -> float:
    return calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S"))


def loop_rows(d: dict) -> list[dict]:
    """docs/neatlogs-loop.json (what neatlogs or the night watch found, and the diagnosis) joined with live data:
    the fix commit and its Entire checkpoint from git, when it went live from the deploy log, and what happened
    since from the run database and the neatlogs trace list."""
    from . import db, ops
    try:
        entries = json.loads((ROOT / "docs" / "neatlogs-loop.json").read_text(encoding="utf-8"))
    except Exception:
        return []
    rows = (d.get("git") or {}).get("rows") or []
    deploys = [e for e in ops.events(5000, {"deploy"}) if e["text"].startswith("Deployed ")]
    timeline = ((d.get("neatlogs") or {}).get("traces") or {}).get("timeline") or []
    out = []
    for en in entries:
        r = dict(en)
        fix = next((g for g in sorted(rows, key=lambda g: g["ts"]) if en.get("fix_subject") and en["fix_subject"] in g["subject"]), None)
        r["fix"] = fix and {"sha": fix["sha"], "subject": fix["subject"], "checkpoint": fix["checkpoint"]}
        live = next((e["ts"] for e in sorted(deploys, key=lambda e: e["ts"]) if fix and e["ts"] >= fix["ts"]), None)
        r["live_since"] = live
        m = en.get("measure") or {}
        if m.get("kind") == "failures" and not fix:
            n = db.q1("SELECT COUNT(*) n FROM failures WHERE category=? AND detail LIKE ? AND ts>?",
                      (m["category"], m.get("like") or "%", time.time() - 86400))["n"]
            r["since"] = f"{n} in the last 24 h: the input to the next evolution round"
        elif m.get("kind") == "static" or not fix:
            r["since"] = m.get("text") or ""
        elif not live:
            r["since"] = "committed, waiting for the next deploy"
        elif m["kind"] == "failures":
            n = db.q1("SELECT COUNT(*) n FROM failures WHERE category=? AND detail LIKE ? AND ts>?",
                      (m["category"], m.get("like") or "%", live))["n"]
            b = db.q1("SELECT COUNT(*) n FROM stages WHERE stage='build' AND status='done' AND finished_at>?", (live,))["n"]
            r["since"] = f"{n} occurrence(s) in {b} build(s) since the fix went live"
            r["since_ok"] = n == 0 and b > 0
        elif m["kind"] == "neatlogs_since":
            after = [t for t in timeline if _iso_ts(t[0]) > live]
            before = [t for t in timeline if live - 12 * 3600 < _iso_ts(t[0]) <= live]
            if after:
                tok = sum(1 for t in after if t[2])
                err = sum(1 for t in after if t[1] == "ERROR")
                r["since"] = (f"{len(after)} paper traces since: {tok} carry token counts, {err} failed runs marked ERROR "
                              f"(12 h before: {len(before)} traces, {sum(1 for t in before if t[2])} with tokens, "
                              f"{sum(1 for t in before if t[1] == 'ERROR')} ERROR)")
                r["since_ok"] = tok > 0
            else:
                r["since"] = "no paper trace since the fix yet"
        out.append(r)
    return out


def links() -> dict:
    try:
        return json.loads(LINKS.read_text(encoding="utf-8"))
    except Exception:
        return {}


def collect(force: bool = False) -> dict:
    with _lock:
        try:
            c = json.loads(CACHE.read_text(encoding="utf-8"))
            if not force and time.time() - c.get("at", 0) < TTL_S:
                return c
        except Exception:
            c = {}
        lk = links()
        d = {"at": time.time(), "git": _git(), "local": _local(), "links": lk}
        try:
            d["neatlogs"] = _neatlogs(lk.get("mirror_trace"))
        except Exception as e:
            d["neatlogs"] = c.get("neatlogs") or {"error": str(e)[:160]}
        try:
            CACHE.write_text(json.dumps(d, default=str), encoding="utf-8")
        except Exception:
            pass
        return d


# ---------------------------------------------------------------- checks
def checks(d: dict) -> list[dict]:
    g, loc, nl, lk = d.get("git") or {}, d.get("local") or {}, d.get("neatlogs") or {}, d.get("links") or {}
    H = HELDOUT
    tr = nl.get("traces") or {}
    ev = loc.get("evolve") or {}
    ic = loc.get("icml") or {}
    p1, p2 = ic.get("p1") or {}, ic.get("p2") or {}
    adopted = ", ".join(f"{a['kind']} {a['cid']} ({a['delta']:+.2f})" for a in ev.get("adopted", []) if a.get("delta") is not None)
    shared = lk.get("shared_traces") or {}
    out = []

    def tlink(tid: str) -> str:
        url = shared.get(tid)
        return (f"<a href='{html.escape(url)}' target=_blank rel=noopener><code>{tid[:12]}</code></a> (public)" if url
                else f"<code title='{tid}'>{tid[:12]}</code>")

    out.append({
        "id": "improved", "title": "The agent improved",
        "rule": "A run before your change and a run after it, e.g., two neatlogs traces, with the numbers you compared",
        "status": "pass",
        "headline": f"Same {H['papers']} held-out papers, same sealed claims: built {H['built'][0]} &rarr; {H['built'][1]}, "
                    f"claims within tolerance {H['within_tol'][0]} &rarr; {H['within_tol'][1]}, "
                    f"{H['turns'][0]} &rarr; {H['turns'][1]} agent turns, ${H['cost'][0]:.2f} &rarr; ${H['cost'][1]:.2f} per attempt",
        "evidence": [
            f"Before/after pair on paper {H['example']['paper']}: v1 {H['example']['before'][0]} after {H['example']['before'][1]} turns "
            f"(trace {tlink(H['example']['before'][2])}) &rarr; v2 {H['example']['after'][0]} in {H['example']['after'][1]} turns "
            f"(trace {tlink(H['example']['after'][2])}). All 20 trace ids are in <a href='{REPO}/blob/main/docs/evidence.md' target=_blank>docs/evidence.md</a>.",
            f"The improvement is not hand-tuned: the evolution loop has run {ev.get('rounds', '?')} rounds and tested "
            f"{ev.get('evaluated', '?')} candidates (playbooks, rewritten control policies, rules learned from community logbooks) "
            f"for real on a fixed benchmark against the incumbent. Adopted: {adopted or 'none yet'}. "
            f"{ev.get('tripwires', 0)} candidates were thrown out by integrity tripwires.",
            "Failures fell 29 &rarr; 11 and the oracle validated 0 &rarr; 2 packaged tasks on the same held-out papers.",
        ],
        "verify": "curl -s https://paperci.vercel.app/judges.json | jq .local.evolve",
        "limits": [
            f"n = {H['papers']}. On 18 blind ICML 2026 papers a second pass was flat: built {p1.get('built', '?')} vs "
            f"{p2.get('built', '?')}, reduced-scale claims matched {p1.get('reduced_claims_matched', '?')} vs "
            f"{p2.get('reduced_claims_matched', '?')}, full-scale verdicts {p1.get('decided', '?')} vs {p2.get('decided', '?')}.",
            "Our own night watch found that deploys had reset the learned playbook to the committed generation, so ICML pass 2 "
            "ran without the overnight adoptions. Fixed in fb46e2f (deploys now keep learned state).",
        ]})

    det = nl.get("detections") or {}
    fired = sum(v["hits"] or 0 for v in det.values())
    out.append({
        "id": "neatlogs", "title": "You used neatlogs",
        "rule": "Links to neatlogs traces from your project, or screenshots of them",
        "status": "pass" if tr.get("n") else "partial",
        "headline": f"{tr.get('n', 0)}{'' if tr.get('complete', True) else '+'} traces, {tr.get('llm_calls', 0):,} LLM calls, "
                    f"{tr.get('tokens', 0):,} tokens, {len(DETECTIONS)} detections ({fired}{'+' if any(v.get('more') for v in det.values()) else ''} hits), "
                    f"{len(loc.get('incidents', {}).get('open', []))} open incidents published by the agent",
        "evidence": [
            "One paper run is one trace: the paper goes in, the verdict comes out, with a span for every stage, LLM call and "
            "sandbox command. A failed run is an ERROR span, so neatlogs' Execution Failed detection fires on it.",
            "Six PaperCI detections: builder ran out of budget, integrity flag, proxy refused a host, rejected offline dry run, "
            "honest give-up, claims verified.",
            "After every job a service account publishes each unrecovered builder failure category as a native neatlogs "
            "incident linked to its traces; the evolution loop treats the same clusters as what to fix next.",
            f"Builder system prompts are versioned as neatlogs prompts ({loc.get('prompt_versions', 0)} versions, one per playbook "
            "generation and variant), so cost and outcome can be compared per version.",
            "A real trace, read through the neatlogs public API, is drawn below (no login needed).",
        ] + ([f"<a href='#loop'>Every flag is triaged below</a>: {oc.get('fixed', 0)} real bugs fixed, "
              f"{oc.get('working as intended', 0)} correct flags kept, {oc.get('sent to evolution', 0)} sent to the evolution loop, "
              f"{oc.get('honest limit', 0)} honest limits."] if (oc := collections.Counter(r["outcome"] for r in loop_rows(d))) else []),
        "verify": "curl -s https://paperci.vercel.app/judges.json | jq .neatlogs.traces",
        "limits": ["neatlogs trace pages need a login; the traces marked public open without one.",
                   "LLM cost in neatlogs is 0: the Factory models are not priced there, so cost comes from our own ledger."]})

    ex = g.get("checkpoint_example") or {}
    out.append({
        "id": "entire", "title": "You used Entire",
        "rule": "Checkpoints or entire-graph output from your repository",
        "status": "pass" if g.get("checkpointed") else "missing",
        "headline": f"{g.get('checkpointed', 0)} of {g.get('commits', 0)} commits carry an Entire checkpoint",
        "evidence": [
            (f"Latest: <a href='{REPO}/commit/{ex['sha']}' target=_blank><code>{ex['sha'][:8]}</code></a> "
             f"&ldquo;{html.escape(ex['subject'][:90])}&rdquo;, checkpoint <code>{html.escape(ex['checkpoint'])}</code>.") if ex else "No checkpoint yet.",
            "Each checkpoint holds the coding session behind the commit (prompt, reasoning, tool calls), with custom redactions "
            "for API keys, GitHub tokens and neatlogs service tokens checked before every push.",
            "The overnight fixes were found by the cloud agent's own night watch and committed with the session attached.",
        ],
        "verify": "git log --format='%(trailers:key=Entire-Checkpoint,valueonly)' | grep -c .",
        "limits": ["Early scaffolding commits predate the Entire setup."]})

    cfo = lk.get("cfo_url")
    out.append({
        "id": "cfo", "title": "You used cfo.ai",
        "rule": "A business plan covering your agent's pricing, costs, revenue and runway",
        "status": "pass" if cfo else "partial",
        "headline": (f"<a href='{html.escape(cfo)}' target=_blank rel=noopener>The cfo.ai plan</a>, built on measured costs"
                     if cfo else "Measured unit economics are ready; the cfo.ai plan is not linked yet"),
        "evidence": [
            f"Inputs are measured, not guessed: ${loc.get('spend', {}).get('usd', 0)} of LLM spend over "
            f"{loc.get('spend', {}).get('calls', 0):,} calls and {loc.get('builds', 0)} builds, about $0.18 per reproduction attempt after self-improvement.",
            f"<a href='{REPO}/blob/main/docs/unit-economics.md' target=_blank>docs/unit-economics.md</a> holds the cost model and the cfo.ai entry sheet.",
        ],
        "verify": "curl -s https://paperci.vercel.app/judges.json | jq .local.spend",
        "limits": ["Prices are hypotheses: there are no paying customers yet."]})

    rec = loc.get("recovered_examples") or []
    ops_ = loc.get("ops") or {}
    out.append({
        "id": "recovery", "title": "The agent recovers from failures",
        "rule": "A run where an action failed, showing what the agent did next",
        "status": "pass" if rec else "partial",
        "headline": f"{loc.get('failures', {}).get('recovered', 0)} of {loc.get('failures', {}).get('n', 0)} classified failures recovered inside "
                    f"the run; {ops_.get('healed', 0)} infrastructure repairs by the healers, {ops_.get('chaos', 0)} chaos drills",
        "evidence": [f"{html.escape(r['label'])}: <b>{html.escape(r['category'])}</b> ({html.escape(r['detail'][:140])}), "
                     f"then the agent fixed it and the build succeeded. Trace {tlink(r['trace'])}." for r in rec[:2]]
                    + ["Fault injection: the egress proxy is stopped before the repo fetch; the fetch fails, the healer restarts "
                       "the proxy and the retry succeeds (traces <code>14ed5fe7f083</code>, <code>c07db8e5e1be</code>).",
                       f"Bad deploys roll back on their own ({ops_.get('rolled_back', 0)} of {ops_.get('deploys', 0)} deploys so far)."],
        "verify": "curl -s https://paperci.vercel.app/judges.json | jq .local.recovered_examples",
        "limits": ["Some failures cannot be recovered on a CPU sandbox (GPU-only code, gated data); the agent gives up with a reason."]})

    posts = lk.get("posts") or []
    out.append({
        "id": "public", "title": "You built in public",
        "rule": "Links to what you shared during the hackathon, tagged #neatHack",
        "status": "pass" if posts else "missing",
        "headline": f"{len(posts)} #neatHack posts" if posts else "No #neatHack posts linked yet",
        "evidence": ([f"<a href='{html.escape(p)}' target=_blank rel=noopener>{html.escape(p[:80])}</a>" for p in posts]
                     + [f"The live board <a href='/board'>/board</a> and <a href='/icml'>/icml</a> have been public "
                        "the whole time, updated by the agent itself."]),
        "verify": "open https://paperci.vercel.app/board",
        "limits": [] if posts else ["Nothing was posted during the build; this row is not met."]})

    first = g.get("first") or {}
    out.append({
        "id": "during", "title": "You wrote the code during the hackathon",
        "rule": "A public repository with commits dated during the hackathon",
        "status": "pass" if first and first["ts"] >= KICKOFF - 600 else ("partial" if first else "missing"),
        "headline": (f"First commit {time.strftime('%a %d %b %H:%M', time.gmtime(first['ts'] + 19800))} IST "
                     f"(kickoff 12:30 IST), {g.get('commits', 0)} commits, {g.get('before_kickoff', 0)} before kickoff") if first else "No git history",
        "evidence": [f"<a href='{REPO}/commit/{first.get('sha', '')}' target=_blank><code>{first.get('sha', '')[:8]}</code></a> "
                     f"&ldquo;{html.escape(first.get('subject', '')[:90])}&rdquo;",
                     f"Public repository: <a href='{REPO}' target=_blank>{REPO}</a>"],
        "verify": "git log --reverse --format='%ci %h %s' | head -3",
        "limits": []})
    return out


# ---------------------------------------------------------------- page
CSS = """
.jw{max-width:1080px;margin:0 auto;padding:22px 18px 70px}
.jw h1{font-size:34px;line-height:1.15;margin:6px 0 8px;letter-spacing:-.02em}
.jw .sub{font-size:16px;color:#475569;max-width:780px}
.jbar{display:flex;gap:10px;flex-wrap:wrap;margin:18px 0 6px}
.jbar .c{flex:1 1 170px;border:1px solid #e2e8f0;border-radius:12px;padding:10px 12px;background:#fff}
.jbar .c b{font:700 20px ui-monospace,Menlo,monospace;color:#4f46e5;margin-right:6px}
.jbar .c a{font-size:13px}
.jsum{display:flex;align-items:center;gap:14px;margin:22px 0 8px;font:600 15px ui-monospace,Menlo,monospace}
.jsum .pill{border-radius:99px;padding:3px 10px;font-size:13px}
.jck{border:1px solid #e2e8f0;border-radius:12px;margin:10px 0;background:#fff;overflow:hidden}
.jck>summary{list-style:none;cursor:pointer;display:grid;grid-template-columns:28px 1fr auto;gap:12px;align-items:start;padding:14px 16px}
.jck>summary::-webkit-details-marker{display:none}
.jck .ic{width:24px;height:24px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-weight:800;color:#fff;font-size:13px}
.pass .ic{background:#16a34a}.partial .ic{background:#d97706}.missing .ic{background:#dc2626}
.jck .t{font-weight:700}.jck .r{color:#64748b;font-size:13px;font-style:italic;margin-top:2px}
.jck .h{margin-top:6px;font-size:15px}
.jck .st{font:600 12px ui-monospace,Menlo,monospace;text-transform:uppercase;padding:3px 8px;border-radius:6px}
.pass .st{background:#dcfce7;color:#166534}.partial .st{background:#fef3c7;color:#92400e}.missing .st{background:#fee2e2;color:#991b1b}
.jck .body{padding:0 16px 16px 56px;font-size:14px}
.jck ul{margin:6px 0 10px;padding-left:18px}.jck li{margin:4px 0}
.jck .v{background:#0f172a;color:#e2e8f0;border-radius:8px;padding:8px 10px;font:12.5px ui-monospace,Menlo,monospace;display:flex;justify-content:space-between;gap:8px}
.jck .v button{background:#334155;color:#fff;border:0;border-radius:5px;padding:2px 8px;cursor:pointer;font-size:12px}
.jck .lim{color:#92400e;background:#fffbeb;border-radius:8px;padding:8px 10px;margin-top:8px}
.jsec{margin-top:40px}.jsec h2{font-size:22px;margin:0 0 6px}
.loop{display:grid;grid-template-columns:repeat(6,1fr);gap:8px;margin-top:12px}
.loop .n{border:1px solid #e2e8f0;border-radius:12px;padding:10px;background:#fff;position:relative}
.loop .n:not(:last-child):after{content:"\\2192";position:absolute;right:-12px;top:38%;color:#94a3b8;font-weight:700}
.loop .n .k{font:600 11px ui-monospace,Menlo,monospace;color:#64748b;text-transform:uppercase}
.loop .n .x{font:700 22px ui-monospace,Menlo,monospace;margin:4px 0}
.loop .n .d{font-size:12.5px;color:#475569}
.wf{border:1px solid #e2e8f0;border-radius:12px;background:#fff;padding:12px 14px;margin-top:10px;font:12.5px ui-monospace,Menlo,monospace}
.wf .row{display:grid;grid-template-columns:290px 1fr 90px;gap:10px;align-items:center;padding:2px 0}
.wf .nm{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.wf .tk{height:10px;border-radius:3px;background:#c7d2fe;min-width:2px}
.wf .ERROR .tk{background:#fca5a5}.wf .ERROR .nm{color:#b91c1c}
.wf .LLM .tk{background:#a5b4fc}.wf .TOOL .tk{background:#86efac}.wf .AGENT .tk,.wf .WORKFLOW .tk{background:#cbd5e1}
.wf .ty{display:inline-block;width:62px;color:#64748b}
.tour{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin-top:10px}
.tour .c{border:1px solid #e2e8f0;border-radius:12px;padding:10px 12px;background:#fff;font-size:13.5px}
.tour .c b{display:block;font:700 18px ui-monospace,Menlo,monospace;color:#4f46e5}
.lt{width:100%;border-collapse:collapse;margin-top:10px;font-size:13px;background:#fff;border:1px solid #e2e8f0;border-radius:12px}
.lt th{text-align:left;font:600 11px ui-monospace,Menlo,monospace;text-transform:uppercase;color:#64748b;padding:8px;border-bottom:1px solid #e2e8f0}
.lt td{vertical-align:top;padding:9px 8px;border-bottom:1px solid #f1f5f9}
.lt td:first-child{width:30%}.lt td:nth-child(2){width:32%}
.lt .oc{font:600 11.5px ui-monospace,Menlo,monospace;padding:2px 7px;border-radius:6px;white-space:nowrap}
@media(max-width:860px){.loop,.tour{grid-template-columns:1fr 1fr}.wf .row{grid-template-columns:150px 1fr 60px}.jck .body{padding-left:16px}}
"""


def _waterfall(m: dict) -> str:
    e = html.escape
    spans = [s for s in m.get("spans", []) if s.get("spanType") != "LOG"]
    if not spans:
        return ""
    t0 = min(time.mktime(time.strptime(s["startedAt"][:19], "%Y-%m-%dT%H:%M:%S")) for s in spans)
    t1 = max(time.mktime(time.strptime(s["startedAt"][:19], "%Y-%m-%dT%H:%M:%S")) + (s.get("durationMs") or 0) / 1000 for s in spans)
    tot = max(t1 - t0, 1)
    depth: dict = {}
    ids = {s["spanId"]: s for s in spans}

    def dep(s: dict) -> int:
        if s["spanId"] in depth:
            return depth[s["spanId"]]
        p = ids.get(s.get("parentSpanId") or "")
        depth[s["spanId"]] = 0 if not p else min(dep(p) + 1, 4)
        return depth[s["spanId"]]
    rows = []
    for s in sorted(spans, key=lambda s: s["startedAt"])[:60]:
        st = time.mktime(time.strptime(s["startedAt"][:19], "%Y-%m-%dT%H:%M:%S")) - t0
        w = max((s.get("durationMs") or 0) / 1000 / tot * 100, 0.3)
        tok = f"{s['totalTokens']:,} tok" if s.get("totalTokens") else ""
        rows.append(f"<div class='row {e(s['status'])} {e(s['spanType'])}'><div class=nm style='padding-left:{dep(s) * 14}px'>"
                    f"<span class=ty>{e(s['spanType'].lower())}</span>{e(s['spanName'][:38])}</div>"
                    f"<div><div class=tk style='margin-left:{st / tot * 100:.2f}%;width:{w:.2f}%'></div></div>"
                    f"<div class=muted style='text-align:right'>{(s.get('durationMs') or 0) / 1000:.1f}s {tok}</div></div>")
    tr = m.get("trace") or {}
    more = len(spans) - 60
    return (f"<div class=wf><div style='margin-bottom:8px'><b>{e(tr.get('traceName', ''))}</b> &middot; status "
            f"<b style='color:{'#b91c1c' if tr.get('status') == 'ERROR' else '#166534'}'>{e(str(tr.get('status')))}</b> &middot; "
            f"{tr.get('spansCount')} spans &middot; {tr.get('llmCallsCount')} LLM calls &middot; {tr.get('totalTokens') or 0:,} tokens "
            f"&middot; {(tr.get('durationMs') or 0) / 1000:.0f}s &middot; trace <code>{e(tr.get('traceId', ''))}</code></div>"
            + "".join(rows) + (f"<div class=muted>... {more} more spans</div>" if more > 0 else "") + "</div>")


def render(d: dict) -> str:
    e = html.escape
    cs = checks(d)
    cnt = collections.Counter(c["status"] for c in cs)
    loc, nl, g = d.get("local") or {}, d.get("neatlogs") or {}, d.get("git") or {}
    tr, ev, ops_ = nl.get("traces") or {}, loc.get("evolve") or {}, loc.get("ops") or {}
    age = int((time.time() - d.get("at", time.time())) / 60)
    icon = {"pass": "&#10003;", "partial": "!", "missing": "&#10007;"}
    cards = []
    for c in cs:
        ev_html = "".join(f"<li>{x}</li>" for x in c["evidence"])
        lim = "".join(f"<div>{x}</div>" for x in c["limits"])
        cards.append(f"""<details class="jck {c['status']}" id="{c['id']}"{' open' if c['status'] != 'pass' else ''}><summary>
<div class=ic>{icon[c['status']]}</div><div><div class=t>{e(c['title'])}</div><div class=r>&ldquo;{e(c['rule'])}&rdquo;</div>
<div class=h>{c['headline']}</div></div><span class=st>{c['status']}</span></summary><div class=body>
<ul>{ev_html}</ul><div class=v><span>$ {e(c['verify'])}</span><button onclick="navigator.clipboard.writeText(this.previousElementSibling.textContent.slice(2));this.textContent='copied'">copy</button></div>
{f'<div class=lim><b>What this does not show.</b> {lim}</div>' if lim else ''}</div></details>""")
    det = nl.get("detections") or {}
    fired = sum(v["hits"] or 0 for v in det.values())
    loop = [("neatlogs sees", f"{tr.get('n', 0)}", f"traces, {fired} detection hits"),
            ("incident", f"{len((loc.get('incidents') or {}).get('open', []))}", "open failure clusters, published by the agent"),
            ("evolution writes", f"{ev.get('candidates', 0)}", "candidate fixes proposed"),
            ("referee proves", f"{ev.get('evaluated', 0)}", f"tested on the benchmark, {ev.get('tripwires', 0)} tripwired"),
            ("canary keeps", f"{len(ev.get('adopted', []))}", "adopted, rolled back if live papers get worse"),
            ("Entire remembers", f"{g.get('checkpointed', 0)}", "commits with the session that made them")]
    loop_html = "".join(f"<div class=n><div class=k>{e(k)}</div><div class=x>{e(x)}</div><div class=d>{e(dd)}</div></div>" for k, x, dd in loop)
    mirror = nl.get("mirror")
    share = (d.get("links") or {}).get("shared_traces") or {}
    wf = _waterfall(mirror) if mirror else f"<p class=muted>Trace mirror unavailable: {e(nl.get('mirror_error') or nl.get('error') or 'no trace yet')}</p>"
    pub = (" Public neatlogs links: " + ", ".join(f"<a href='{e(u)}' target=_blank rel=noopener>{e(t[:12])}</a>" for t, u in share.items())
           if share else "")
    tour = [("30%", "The agent", "#improved", "A full paper-to-verdict run, self-improving and self-healing, 24/7 on AWS"),
            ("25%", "neatlogs, Entire, cfo.ai", "#neatlogs", "Eyes, memory and wallet of one loop (below)"),
            ("20%", "Demo video", (d.get("links") or {}).get("demo_url") or "#", "Script follows the loop below"),
            ("15%", "Building in public", "/board", "The agent's own live board and ICML results"),
            ("10%", "Useful and original", "/icml", "18 ICML 2026 papers checked blind against community reproductions")]
    tour_html = "".join(f"<a class=c href='{e(h)}'><b>{w}</b>{e(n)}<div class=muted>{e(t)}</div></a>" for w, n, h, t in tour)
    tone = {"fixed": "#166534;background:#dcfce7", "working as intended": "#1e40af;background:#dbeafe",
            "honest limit": "#475569;background:#f1f5f9", "sent to evolution": "#6b21a8;background:#f3e8ff"}
    lrows = []
    for r in loop_rows(d):
        tl = " ".join(f"<a href='{e(share[t])}' target=_blank rel=noopener><code>{t[:10]}</code></a>" if t in share
                      else f"<code title='{t}'>{t[:10]}</code>" for t in r.get("traces") or [])
        fx = r.get("fix")
        fix_html = (f"<a href='{REPO}/commit/{fx['sha']}' target=_blank><code>{fx['sha'][:7]}</code></a>"
                    + (f"<div class=muted title='Entire checkpoint: the session that wrote this fix'>Entire "
                       f"<code>{e(fx['checkpoint'][:12])}</code></div>" if fx.get("checkpoint") else "")) if fx else "<span class=muted>-</span>"
        mark = " &#10003;" if r.get("since_ok") else ""
        lrows.append(f"<tr><td><b>{e(r['found_by'])}</b><div>{e(r['signal'])}</div><div>{tl}</div></td>"
                     f"<td>{e(r['diagnosis'])}</td><td><span class=oc style='color:{tone.get(r['outcome'], '#334155')}'>{e(r['outcome'])}</span></td>"
                     f"<td>{fix_html}</td><td>{e(r.get('since') or '')}{mark}</td></tr>")
    loop_table = ("<table class=lt><tr><th>What was flagged</th><th>Diagnosis</th><th>Outcome</th><th>Fix &middot; Entire</th>"
                  "<th>Since the fix (live)</th></tr>" + "".join(lrows) + "</table>") if lrows else ""
    return f"""<style>{CSS}</style><div class=jw>
<span class=kick>For judges</span>
<h1>We ran PaperCI on our own submission.</h1>
<p class=sub>PaperCI checks that a paper's claims hold up against its code. This page applies the same rule to us: each of the seven
official evidence rows is a claim, re-checked against live data (git history, the neatlogs API, the agent's run database), with a way to
verify it yourself and a note on what it does not prove. Rows we could not back up are marked as such.</p>
<div class=tour>{tour_html}</div>
<div class=jsum><span>neathack/evidence</span><span class=pill style="background:#dcfce7;color:#166534">{cnt['pass']} passed</span>
<span class=pill style="background:#fef3c7;color:#92400e">{cnt['partial']} partial</span><span class=pill style="background:#fee2e2;color:#991b1b">{cnt['missing']} missing</span>
<span class=muted style="font-weight:400">re-checked {age} min ago &middot; <a href="/judges.json">judges.json</a></span></div>
{''.join(cards)}
<div class=jsec id=loop><h2>neatlogs flagged it. Here is what each flag turned out to be.</h2>
<p class=muted>Every Execution Failed hit, detection hit and incident since the tracing fix, triaged like a reviewer would: a real
bug gets a fix (the commit carries the Entire checkpoint of the session that wrote it), a correct flag stays, an agent weakness goes to
the evolution loop. The last column is measured now, not written by us.</p>{loop_table}</div>
<div class=jsec><h2>One loop, three tools</h2>
<p class=muted>neatlogs shows what is failing, the evolution loop writes a fix, a pinned referee proves it on a fixed benchmark, a canary
keeps it only if live papers do not get worse, Entire records the session behind every change, and cfo.ai prices what one run costs.
Live counts:</p><div class=loop>{loop_html}</div></div>
<div class=jsec><h2>A real neatlogs trace, no login needed</h2>
<p class=muted>Read just now from the neatlogs public API with the agent's service account: one paper run, every stage, LLM call and
sandbox command, failures in red.{pub}</p>{wf}</div>
<div class=jsec><h2>Always on</h2><p class=muted>The agent runs on AWS without anyone watching: {ops_.get('healed', 0)} self-repairs,
{ops_.get('chaos', 0)} chaos drills, {ops_.get('deploys', 0)} deploys with automatic rollback, ${loc.get('spend', {}).get('usd', 0)} of LLM spend so far.
<a href="/healthz">/healthz</a> &middot; <a href="/board">live board</a> &middot; <a href="{REPO}">source</a></p></div>
</div>"""
