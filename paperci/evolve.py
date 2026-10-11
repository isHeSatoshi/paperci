"""Safe recursive self-improvement for the builder agent.

Each night one round runs, one step at a time inside the web worker (author submissions always go first):

  1. canary     the last adopted change is checked against live autopilot results; worse -> automatic rollback
  2. meta       (the recursive part, depth 1) pick an improver *strategy* by how much its past proposals
                actually helped; occasionally an LLM writes a new strategy from that scoreboard
  3. propose    candidate A: a new playbook (rules + controller knobs) from the improver under that strategy
                candidate B: a rewritten builder control policy (paperci/stages/builder_policy.py contract)
  4. gate       static checks: integrity filter, allowlisted hosts, regex precision, policy contract test
  5. evaluate   real build + offline-verification runs of the incumbent and each candidate on a fixed dev bench
  6. decide     adopt only if measurably better with no regression and no integrity flag; playbooks are adopted
                automatically, policy (code) changes wait for a human `approve`

What can never change: the verifier, claim sealing, tolerances, the cost ledger, this file, the gate and the
held-out split. Their hashes are pinned when a round starts and re-checked after every evaluation run; any
change voids the round. Candidates are data plus one sandboxed policy file, so they have no way to write code.

    python -m paperci.evolve start | step | status | pending | approve <id> | reject <id> | eval <who> <paper> <label>
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from . import db, ledger, llm, ops, policy_sandbox, tracing
from .config import DATA_DIR, MODELS, ROOT
from .improver import BANNED, _evidence, improve
from .recovery import PLAYBOOK_PATH, load_playbook, save_playbook
from .schemas import Playbook, PlaybookRule

EVO = DATA_DIR / "evolve"
CANDS = EVO / "candidates"
BACKUPS = EVO / "backups"
ARCHIVE = EVO / "archive.json"
ROUND = EVO / "round.json"
BENCH = [p.strip() for p in os.getenv("PAPERCI_EVOLVE_BENCH", "2503.01072,2501.14570").split(",") if p.strip()]
BUDGET = float(os.getenv("PAPERCI_EVOLVE_BUDGET", "1.50"))
EST_RUN = 0.25           # conservative cost of one build + offline verification (measured mean ~$0.17)
EVAL_TIMEOUT = 75 * 60
MARGIN = 1.0             # fitness points a candidate must gain over the incumbent on the bench
FROZEN = ["paperci/stages/verifier.py", "paperci/stages/claims.py", "paperci/stages/tasks.py", "paperci/ledger.py",
          "paperci/evolve.py", "paperci/policy_sandbox.py", "paperci/improver.py", "paperci/pipeline.py",
          "paperci/config.py", "paperci/db.py", "sandbox/proxy/allowlist"]
SEED_STRATEGIES = [
    {"id": "s-base", "text": ""},
    {"id": "s-turns", "text": "Optimise for reaching a working /work/paperci_run.sh in fewer turns: prefer rules and "
                              "controller settings that cut exploration; one honestly measured metric at reduced "
                              "scale is a success."},
    {"id": "s-env", "text": "Optimise environment setup: dependency resolution, CPU-only fallbacks and downloading "
                            "data during setup. Do not give advice about the experiment itself."},
]

POLICY_SYSTEM = """You improve the CONTROL POLICY of an autonomous agent ("builder") that reproduces research
papers' code in a CPU-only sandbox. The policy is a small Python file whose functions the harness calls every
turn: system_addendum(state) -> str, milestone(state) -> None | {"key": str, "message": str},
late_nudge(state) -> None | str. A milestone message is injected as a mandatory instruction (each key fires
at most once per build). The builder succeeds when it writes /work/paperci_run.sh that recomputes the paper's
metrics with the repository's own code and passes an offline run.

HARD CONTRACT (violations are rejected automatically):
- No imports (the `re` module is available as `re`), no while loops, no lambda, no try, no classes, no `**`,
  no names or attributes starting with "_", no range/open/eval/getattr/print, numeric constants <= 10000.
- Only these state keys: turn, max_turns, run_by_turn, script_by_turn, single_target, controller,
  submit_attempts, fired, actions (dicts with type, cmd, exit, path), failures (failure-class names).
- Allowed methods: get lower upper strip startswith endswith split join count replace find append items keys
  values, and re.search/match/fullmatch/findall.
- Never instruct the builder to hardcode, guess, fake or copy results or to skip verification.
Return the COMPLETE new file in one ```python block, then one line starting with RATIONALE:."""

META_SYSTEM = """You tune the strategy of an improver agent. The improver reads failure clusters of a
research-reproduction agent and proposes recovery rules. Each strategy is one instruction (<= 400 chars) added
to the improver's prompt. You get every strategy with how often it was used and the mean measured fitness gain
of the candidates it produced (positive = the agent got better on a fixed benchmark). Write ONE new strategy
that is likely to beat the best one: build on what worked, avoid what did not. Never suggest hardcoding,
faking, guessing results or weakening verification. Answer as: STRATEGY: <text> then RATIONALE: <text>."""


# ---------------------------------------------------------------- state
def _load(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save(p: Path, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, p)


def archive() -> dict:
    a = _load(ARCHIVE, {})
    a.setdefault("strategies", [dict(s, uses=0, gain=0.0, parent=None, created=time.time()) for s in SEED_STRATEGIES])
    a.setdefault("candidates", {})
    a.setdefault("baselines", {})
    a.setdefault("adoptions", [])
    a.setdefault("rounds", [])
    return a


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16] if p.exists() else "-"


def referee() -> dict:
    """Fingerprint of everything a candidate must never influence, including the sealed claims themselves."""
    h = {f: _sha(ROOT / f) for f in FROZEN}
    rows = db.q("SELECT arxiv_id, sha256 FROM sealed_claims ORDER BY arxiv_id")
    h["sealed_claims"] = hashlib.sha256("".join(f"{r['arxiv_id']}:{r['sha256']}" for r in rows).encode()).hexdigest()[:16]
    return h


def incumbent() -> dict:
    pb = PLAYBOOK_PATH.read_text(encoding="utf-8") if PLAYBOOK_PATH.exists() else Playbook().model_dump_json()
    pol = policy_sandbox.load_active()
    key = hashlib.sha256((pb + pol.source).encode()).hexdigest()[:12]
    return {"key": key, "generation": load_playbook().generation, "policy_id": pol.id, "policy_origin": pol.origin}


def round_spend(rid: str) -> float:
    r = db.q1("SELECT COALESCE(SUM(cost_usd),0) s FROM spend WHERE run_label LIKE ?", (f"evo-{rid}%",))
    return float(r["s"]) if r else 0.0


# ---------------------------------------------------------------- meta level (strategies)
def _mean(s: dict) -> float:
    return s["gain"] / s["uses"] if s["uses"] else 0.0


def pick_strategy(a: dict) -> dict:
    """UCB1 over measured gains: untried strategies first, then the best mean with an exploration bonus."""
    untried = [s for s in a["strategies"] if not s["uses"]]
    if untried:
        return untried[0]
    n = sum(s["uses"] for s in a["strategies"])
    return max(a["strategies"], key=lambda s: _mean(s) + 2.0 * math.sqrt(2 * math.log(n) / s["uses"]))


def meta_propose(a: dict, rid: str) -> dict | None:
    """The improver improving its own instructions, judged later by the same benchmark (depth 1: this meta
    prompt itself is fixed)."""
    scored = [s for s in a["strategies"] if s["uses"]]
    if len(scored) < 2:
        return None
    board = "\n".join(f"- {s['id']} used {s['uses']}x, mean gain {_mean(s):+.2f}: {s['text'] or '(no extra instruction)'}"
                      for s in sorted(a["strategies"], key=_mean, reverse=True))
    recent = "\n".join(f"- {c['kind']} candidate via {c['strategy']}: {c['status']} ({c.get('reason', '')[:120]})"
                       for c in list(a["candidates"].values())[-8:])
    resp = llm.chat(arxiv_id="-", stage="evolve_meta", run_label=f"evo-{rid}-propose", model=MODELS["builder"],
                    messages=[{"role": "system", "content": META_SYSTEM},
                              {"role": "user", "content": f"STRATEGIES:\n{board}\n\nRECENT CANDIDATES:\n{recent}"}],
                    max_tokens=500)
    text = resp.choices[0].message.content or ""
    m = re.search(r"STRATEGY:\s*(.+?)(?:\n\s*RATIONALE:|$)", text, re.S)
    if not m:
        return None
    s = " ".join(m.group(1).split())[:400]
    if BANNED.search(s) or policy_sandbox.BANNED_TEXT.search(s) or any(s == x["text"] for x in a["strategies"]):
        ops.event("learn", "Meta level proposed an improver strategy that failed the integrity filter; discarded")
        return None
    best = max(scored, key=_mean)
    new = {"id": f"s-{rid}", "text": s, "uses": 0, "gain": 0.0, "parent": best["id"], "created": time.time()}
    a["strategies"].append(new)
    if len(a["strategies"]) > 8:  # keep the archive small: drop the worst well-tested strategy
        worst = min((x for x in a["strategies"] if x["uses"] >= 2 and x["id"] != "s-base"), key=_mean, default=None)
        if worst:
            a["strategies"].remove(worst)
    return new


# ---------------------------------------------------------------- proposals
def _labels() -> list[str]:
    return [r["run_label"] for r in db.q("SELECT DISTINCT run_label FROM stages WHERE stage='build'")]


def _policy_evidence(clusters: dict) -> str:
    lines = []
    for cat, xs in sorted(clusters.items(), key=lambda kv: -len(kv[1]))[:8]:
        lines.append(f"## {cat}: {len(xs)} occurrences, {sum(x['recovered'] for x in xs)} in builds that succeeded")
        lines += [f"- {x['detail'][-300:]!r}" for x in xs[:2]]
    rows = db.q("SELECT s.output FROM stages s JOIN papers p ON p.arxiv_id=s.arxiv_id WHERE s.stage='build' "
                "AND s.status='done' AND p.split='dev'")
    outs = [json.loads(r["output"]) for r in rows]
    by: dict[str, list[int]] = {}
    for o in outs:
        by.setdefault(o.get("status", "?"), []).append(o.get("turns") or 0)
    lines.append("## dev build outcomes: " + ", ".join(f"{k} x{len(v)} (mean {sum(v) / len(v):.1f} turns)" for k, v in by.items()))
    return "\n".join(lines)


def propose_policy(clusters: dict, strategy: dict, rid: str) -> tuple[str | None, str]:
    cur = policy_sandbox.load_active()
    user = (f"STRATEGY: {strategy['text'] or 'general improvement'}\n\nEVIDENCE FROM DEV-SPLIT BUILDS:\n"
            f"{_policy_evidence(clusters)[:9000]}\n\nCURRENT POLICY ({cur.name}):\n```python\n{cur.source}\n```")
    msgs = [{"role": "system", "content": POLICY_SYSTEM}, {"role": "user", "content": user}]
    for attempt in range(2):  # one repair round with the contract violations
        resp = llm.chat(arxiv_id="-", stage="evolve_policy", run_label=f"evo-{rid}-propose", model=MODELS["builder"],
                        messages=msgs, max_tokens=3500)
        text = resp.choices[0].message.content or ""
        m = re.search(r"```python\n(.*?)```", text, re.S)
        why = (re.search(r"RATIONALE:\s*(.+)", text) or [None, ""])[1][:300]
        if not m:
            problems = ["no ```python block in the answer"]
        else:
            src = m.group(1).strip() + "\n"
            if policy_sandbox.policy_id(src) == cur.id:
                return None, "proposal identical to the current policy"
            problems = policy_sandbox.contract_test(src)
            if not problems:
                return src, why
        msgs += [{"role": "assistant", "content": text},
                 {"role": "user", "content": "The policy was rejected:\n- " + "\n- ".join(problems[:8]) + "\nFix it and resend the complete file."}]
    return None, "contract: " + "; ".join(problems[:3])


def propose_playbook(labels: list[str], strategy: dict, rid: str) -> tuple[Playbook | None, str, dict]:
    out = improve(labels, apply=False, only_split="dev", strategy=strategy["text"], run_label=f"evo-{rid}-propose")
    pb = load_playbook()
    acc = out.get("accepted") or []
    ctrl_ok = bool(out.get("controller_gate") and out["controller_gate"][0])
    if not acc and not ctrl_ok:
        rej = "; ".join(f"{r['rule']['rule_id']}: {r['gate']}" for r in (out.get("rejected") or [])[:3])
        return None, f"no proposal passed the static gate ({rej or out.get('status', 'nothing proposed')})", out
    by_id = {r.rule_id: r for r in pb.rules}
    for x in acc:
        by_id[x["rule"]["rule_id"]] = PlaybookRule.model_validate(x["rule"])
    from .schemas import Controller
    cand = Playbook(generation=pb.generation + 1, rules=list(by_id.values()), image=pb.image,
                    controller=Controller.model_validate(out["controller"]) if ctrl_ok else pb.controller,
                    notes=f"candidate from round {rid} via strategy {strategy['id']}")
    return cand, f"{len(acc)} rule(s){' + controller' if ctrl_ok else ''} passed the static gate", out


# ---------------------------------------------------------------- canary + rollback
def canary(a: dict) -> None:
    """The bench is small, so every adoption is re-checked on live autopilot papers it never saw."""
    for ad in a["adoptions"]:
        if ad.get("state") != "watching":
            continue
        rows = db.q("SELECT output, finished_at FROM stages WHERE stage='build' AND status='done' AND run_label LIKE 'live-%'")
        outs = [(json.loads(r["output"]), r["finished_at"] or 0) for r in rows]
        match = (lambda o: o.get("playbook_generation") == ad["generation"]) if ad["kind"] == "playbook" else \
            (lambda o: o.get("policy_id") == ad["policy_id"])
        after = [o for o, t in outs if t >= ad["at"] and match(o)]
        before = [o for o, t in outs if ad["at"] - 7 * 86400 <= t < ad["at"]]
        if len(after) < 3:
            continue
        rate = sum(o.get("status") == "built" for o in after) / len(after)
        prev = sum(o.get("status") == "built" for o in before) / len(before) if before else 0.0
        if before and rate + 0.34 < prev:
            Path(ad["restore_to"]).write_text(Path(ad["backup"]).read_text(encoding="utf-8"), encoding="utf-8") \
                if ad.get("backup") else Path(ad["restore_to"]).unlink(missing_ok=True)
            ad["state"] = "rolled_back"
            ops.event("incident", f"Canary: adopted {ad['kind']} {ad['cid']} built {rate:.0%} of {len(after)} live papers vs "
                                  f"{prev:.0%} before; rolled back automatically", healed=True, component="evolve")
        else:
            ad["state"] = "confirmed"
            ops.event("learn", f"Canary: adopted {ad['kind']} {ad['cid']} confirmed on {len(after)} live papers "
                               f"(built {rate:.0%} vs {prev:.0%} before)")


# ---------------------------------------------------------------- round lifecycle
def start() -> dict:
    """Nightly entry point: canary check, then propose and queue one round (cheap; evaluation runs later, stepwise)."""
    a = archive()
    canary(a)
    cur = _load(ROUND, {})
    _save(ARCHIVE, a)
    if cur and cur.get("status") not in ("done", None):
        return {"status": "round already in progress", "round": cur["id"]}
    if cur and time.time() - cur.get("started", 0) < 20 * 3600 and os.getenv("PAPERCI_EVOLVE_FORCE") != "1":
        return {"status": "a round already ran in the last 20 h (one budget per night)", "round": cur["id"]}
    rid = time.strftime("%Y%m%d-%H%M", ops._local())
    bench = [p for p in BENCH if db.q1("SELECT 1 FROM sealed_claims WHERE arxiv_id=?", (p,))]
    if not bench:
        ops.event("learn", "Evolution round skipped: no benchmark paper has sealed claims on this host")
        return {"status": "no bench"}
    labels = _labels()
    clusters = _evidence(labels, only_split="dev")
    if not clusters:
        ops.event("learn", "Evolution round skipped: no dev-split failures to learn from yet")
        return {"status": "no evidence"}
    os.environ["PAPERCI_EVOLVE_JOB"] = "1"  # proposals count toward this round's own budget, not the daily cap
    with tracing.span(f"evolve-round:{rid}", kind="WORKFLOW", bench=bench, budget=BUDGET) as root:
        meta = None
        try:
            meta = meta_propose(a, rid)
        except Exception as ex:
            tracing.log("meta level failed: {err}", level="warning", err=f"{type(ex).__name__}: {ex}"[:200])
        strat = meta or pick_strategy(a)
        tracing.set_attr(root, "strategy", strat["id"])
        inc = incumbent()
        cands = []
        try:
            pb, why_a, _ = propose_playbook(labels, strat, rid)
        except Exception as ex:  # a failed proposal is a rejected candidate, never a crashed round
            pb, why_a = None, f"proposal failed: {type(ex).__name__}: {str(ex)[:160]}"
        cands.append(_register(a, rid, "playbook", strat, pb.model_dump_json(indent=2) if pb else None, why_a))
        try:
            src, why_b = propose_policy(clusters, strat, rid)
        except Exception as ex:
            src, why_b = None, f"proposal failed: {type(ex).__name__}: {str(ex)[:160]}"
        cands.append(_register(a, rid, "policy", strat, src, why_b))
        queue = []
        cached = a["baselines"].get(inc["key"], {})
        queue += [{"who": "incumbent", "paper": p, "status": "pending", "attempts": 0} for p in bench if p not in cached]
        for c in cands:
            if c["status"] == "proposed":
                queue += [{"who": c["id"], "paper": p, "status": "pending", "attempts": 0} for p in bench]
        rnd = {"id": rid, "started": time.time(), "status": "evaluating" if queue else "deciding", "bench": bench,
               "budget": BUDGET, "referee": referee(), "incumbent": inc, "strategy": strat["id"],
               "meta_new_strategy": bool(meta), "candidates": [c["id"] for c in cands], "queue": queue}
        _save(ROUND, rnd)
        _save(ARCHIVE, a)
        tracing.set_attr(root, "queued_runs", len(queue))
    live_c = [c for c in cands if c["status"] == "proposed"]
    ops.event("learn", f"Evolution round {rid}: strategy {strat['id']}{' (new, written by the meta level)' if meta else ''}; "
                       f"{len(live_c)} candidate(s) passed the static gates "
                       f"({', '.join(c['kind'] for c in live_c) or 'none'}); {len(queue)} benchmark run(s) queued, "
                       f"budget ${BUDGET:.2f}", round=rid, strategy=strat["id"])
    if not queue:
        decide()
    return {"status": "started", "round": rid, "queued": len(queue), "candidates": [c["id"] for c in cands]}


def _register(a: dict, rid: str, kind: str, strat: dict, content: str | None, why: str) -> dict:
    cid = f"{rid}-{'pb' if kind == 'playbook' else 'pol'}"
    c = {"id": cid, "round": rid, "kind": kind, "strategy": strat["id"], "created": time.time(), "reason": why,
         "status": "proposed" if content else "rejected_static"}
    if content:
        d = CANDS / cid
        d.mkdir(parents=True, exist_ok=True)
        (d / ("playbook.json" if kind == "playbook" else "builder_policy.py")).write_text(content, encoding="utf-8")
        if kind == "policy":
            c["policy_id"] = policy_sandbox.policy_id(content)
    a["candidates"][cid] = c
    return c


def has_pending() -> bool:
    r = _load(ROUND, {})
    return r.get("status") in ("evaluating", "deciding")


def _env_for(who: str, label: str, rid: str) -> dict:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PAPERCI_EVAL_LIGHT": "1", "PAPERCI_EVOLVE_JOB": "1",
           "PAPERCI_END_USER": "evolve", "PAPERCI_SESSION": f"run:evo-{rid}"}
    env.pop("PAPERCI_PLAYBOOK_PATH", None)
    env.pop("PAPERCI_POLICY_PATH", None)
    if who != "incumbent":
        d = CANDS / who
        if (d / "playbook.json").exists():
            env["PAPERCI_PLAYBOOK_PATH"] = str(d / "playbook.json")
        if (d / "builder_policy.py").exists():
            env["PAPERCI_POLICY_PATH"] = str(d / "builder_policy.py")
    return env


def fitness(paper: str, label: str) -> dict | None:
    b = db.q1("SELECT output FROM stages WHERE arxiv_id=? AND run_label=? AND stage='build' AND status='done'", (paper, label))
    v = db.q1("SELECT output FROM stages WHERE arxiv_id=? AND run_label=? AND stage='verdict' AND status='done'", (paper, label))
    if not b:
        return None
    build = json.loads(b["output"])
    dec = json.loads(v["output"]) if v else {}
    ver = db.q1("SELECT output FROM stages WHERE arxiv_id=? AND run_label=? AND stage='verify' AND status='done'", (paper, label))
    runs = json.loads(ver["output"]).get("runs", []) if ver else []
    pcs = (dec.get("per_claim") or {}).values()
    found = sum(1 for pc in pcs if pc.get("comparison"))
    within = sum(1 for pc in pcs if (pc.get("comparison") or {}).get("within"))
    built = build.get("status") == "built"
    offline_ok = any(r.get("exit_code") == 0 for r in runs)
    turns, cost = build.get("turns") or 0, float(build.get("cost_usd") or 0)
    flags = (dec.get("integrity") or {}).get("flags") or []
    f = 2 * built + 1 * offline_ok + min(found, 3) + 2 * within - 0.05 * turns - 2 * cost
    return {"fitness": round(f, 3), "built": built, "offline_ok": offline_ok, "found": found, "within": within,
            "turns": turns, "cost": round(cost, 4), "flags": flags, "status": build.get("status"),
            "verdict": dec.get("verdict"), "label": label}


def step() -> dict:
    """Run the next queued benchmark run (or the decision). One step per worker slot so authors never wait long."""
    rnd = _load(ROUND, {})
    if rnd.get("status") not in ("evaluating", "deciding"):
        return {"status": "idle"}
    if referee() != rnd["referee"]:
        return _void(rnd, "the referee changed during the round (code deploy or tampering); results are not comparable")
    if rnd["status"] == "deciding":
        return decide()
    pending = [q for q in rnd["queue"] if q["status"] == "pending"]
    if not pending:
        rnd["status"] = "deciding"
        _save(ROUND, rnd)
        return decide()
    item = pending[0]
    spent = round_spend(rnd["id"])
    if spent + EST_RUN > rnd["budget"]:
        for q in pending:
            q["status"] = "skipped_budget"
        rnd["status"] = "deciding"
        _save(ROUND, rnd)
        ops.event("budget", f"Evolution round {rnd['id']}: nightly budget reached (${spent:.2f} of ${rnd['budget']:.2f}); "
                            f"{len(pending)} benchmark run(s) skipped")
        return decide()
    item["attempts"] += 1
    if item["attempts"] > 2:  # poison guard
        item["status"] = "failed"
        _save(ROUND, rnd)
        return {"status": "item failed twice", "item": item}
    _save(ROUND, rnd)
    label = f"evo-{rnd['id']}-{'inc' if item['who'] == 'incumbent' else item['who'].rsplit('-', 1)[-1]}"
    log = ops.OPS / f"evolve-{rnd['id']}-{len(rnd['queue']) - len(pending)}.log"
    p = subprocess.Popen([sys.executable, "-u", "-m", "paperci.evolve", "eval", item["who"], item["paper"], label],
                         cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                         errors="replace", env=_env_for(item["who"], label, rnd["id"]), start_new_session=os.name != "nt")
    try:
        out, _ = p.communicate(timeout=EVAL_TIMEOUT)
    except subprocess.TimeoutExpired:
        ops.kill_tree(p)
        out = (p.communicate()[0] or "") + f"\nkilled after {EVAL_TIMEOUT // 60} min"
    log.write_text(out, encoding="utf-8")
    rnd = _load(ROUND, rnd)
    if referee() != rnd["referee"]:
        return _void(rnd, f"a frozen file or the sealed claims changed during evaluation of {item['who']}")
    res = fitness(item["paper"], label)
    for q in rnd["queue"]:
        if q["who"] == item["who"] and q["paper"] == item["paper"]:
            q.update(status="done" if res else ("pending" if q["attempts"] < 2 else "failed"), result=res)
    if res and item["who"] == "incumbent":
        a = archive()
        a["baselines"].setdefault(rnd["incumbent"]["key"], {})[item["paper"]] = res
        _save(ARCHIVE, a)
    _save(ROUND, rnd)
    return {"status": "ran", "who": item["who"], "paper": item["paper"], "result": res}


def _void(rnd: dict, why: str) -> dict:
    rnd["status"] = "done"
    rnd["outcome"] = f"void: {why}"
    _save(ROUND, rnd)
    a = archive()
    for cid in rnd["candidates"]:
        if a["candidates"].get(cid, {}).get("status") == "proposed":
            a["candidates"][cid].update(status="void", reason=why)
    _save(ARCHIVE, a)
    ops.event("incident", f"Evolution round {rnd['id']} voided: {why}", healed=True, component="evolve")
    return {"status": "void", "why": why}


def decide() -> dict:
    rnd = _load(ROUND, {})
    a = archive()
    inc = a["baselines"].get(rnd["incumbent"]["key"], {})
    strat = next((s for s in a["strategies"] if s["id"] == rnd["strategy"]), None)
    summary = []
    pb_adopted = False
    for cid in rnd["candidates"]:
        c = a["candidates"][cid]
        if c["status"] == "rejected_static":
            if strat:
                strat["uses"] += 1
                strat["gain"] -= 1.0  # a strategy that only yields unusable proposals is worse than doing nothing
            summary.append(f"{c['kind']} rejected by the static gate")
            continue
        if c["status"] != "proposed":
            continue
        res = {q["paper"]: q.get("result") for q in rnd["queue"] if q["who"] == cid}
        papers = [p for p in rnd["bench"] if res.get(p) and inc.get(p)]
        if len(papers) < len(rnd["bench"]):
            c.update(status="unevaluated", reason="not every benchmark run finished (budget or failures)")
            summary.append(f"{c['kind']} not fully evaluated")
            continue
        flags = sorted({f for p in papers for f in res[p]["flags"]})
        cf, inf = sum(res[p]["fitness"] for p in papers), sum(inc[p]["fitness"] for p in papers)
        delta = round(cf - inf, 3)
        regress = [p for p in papers if inc[p]["built"] and not res[p]["built"]]
        c.update(fitness=cf, incumbent_fitness=inf, delta=delta, per_paper=res, evaluated=time.time())
        if strat:
            strat["uses"] += 1
            strat["gain"] += max(-5.0, min(5.0, delta))
        if flags:
            c.update(status="rejected", reason=f"integrity flags on its runs: {flags}")
            ops.event("incident", f"Tripwire: evolved {c['kind']} {cid} triggered integrity flags {flags} on the bench; "
                                  "rejected and kept for inspection", healed=True, component="evolve")
            summary.append(f"{c['kind']} tripped integrity flags")
        elif regress or delta < MARGIN:
            c.update(status="rejected", reason=(f"regressed on {len(regress)} paper(s)" if regress else
                                                f"fitness {delta:+.2f} below the +{MARGIN} margin"))
            summary.append(f"{c['kind']} {delta:+.2f} (not adopted)")
        elif c["kind"] == "playbook":
            _adopt_playbook(a, c)
            pb_adopted = True
            summary.append(f"playbook {delta:+.2f} adopted as generation {load_playbook().generation}")
        else:
            c.update(status="pending_approval", reason=f"fitness {delta:+.2f}; code change waits for a human")
            summary.append(f"policy {delta:+.2f}, awaiting human approval")
    rnd.update(status="done", outcome="; ".join(summary) or "nothing to decide", spent=round(round_spend(rnd["id"]), 4),
               finished=time.time())
    a["rounds"] = (a["rounds"] + [{k: rnd.get(k) for k in ("id", "strategy", "outcome", "spent", "finished",
                                                          "meta_new_strategy")}])[-30:]
    _save(ROUND, rnd)
    _save(ARCHIVE, a)
    ops.event("learn", f"Evolution round {rnd['id']} decided (${rnd['spent']:.2f}): {rnd['outcome']}",
              round=rnd["id"], adopted=pb_adopted)
    return {"status": "decided", "outcome": rnd["outcome"]}


def _backup(src: Path, tag: str) -> str | None:
    if not src.exists():
        return None
    BACKUPS.mkdir(parents=True, exist_ok=True)
    dst = BACKUPS / f"{tag}-{src.name}"
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return str(dst)


def _adopt_playbook(a: dict, c: dict) -> None:
    backup = _backup(PLAYBOOK_PATH, c["id"])
    pb = Playbook.model_validate_json((CANDS / c["id"] / "playbook.json").read_text(encoding="utf-8"))
    pb.notes = f"gen {pb.generation} adopted from {c['id']} (+{c['delta']:.2f} on the bench) at {time.strftime('%Y-%m-%d %H:%M')}"
    save_playbook(pb)
    c["status"] = "adopted"
    a["adoptions"].append({"kind": "playbook", "cid": c["id"], "at": time.time(), "generation": pb.generation,
                           "backup": backup, "restore_to": str(PLAYBOOK_PATH), "state": "watching"})


def approve(cid: str) -> str:
    a = archive()
    c = a["candidates"].get(cid)
    if not c or c["status"] != "pending_approval":
        return f"{cid}: not awaiting approval"
    src = (CANDS / cid / "builder_policy.py").read_text(encoding="utf-8")
    problems = policy_sandbox.contract_test(src)
    if problems:
        return f"{cid}: fails the contract now: {problems[:3]}"
    backup = _backup(policy_sandbox.ACTIVE_PATH, cid)
    policy_sandbox.ACTIVE_PATH.write_text(src, encoding="utf-8")
    c.update(status="adopted", approved=time.time())
    a["adoptions"].append({"kind": "policy", "cid": cid, "at": time.time(), "policy_id": c["policy_id"],
                           "backup": backup, "restore_to": str(policy_sandbox.ACTIVE_PATH), "state": "watching"})
    _save(ARCHIVE, a)
    ops.event("learn", f"Human approved evolved control policy {cid} ({c['delta']:+.2f} on the bench); live from the next "
                       "build, watched by the canary", candidate=cid)
    return f"{cid}: approved and live"


def reject(cid: str) -> str:
    a = archive()
    c = a["candidates"].get(cid)
    if not c or c["status"] != "pending_approval":
        return f"{cid}: not awaiting approval"
    c.update(status="rejected", reason="rejected by a human")
    _save(ARCHIVE, a)
    ops.event("learn", f"Human rejected evolved control policy {cid}")
    return f"{cid}: rejected"


def status() -> dict:
    a, r = archive(), _load(ROUND, {})
    return {"round": {k: r.get(k) for k in ("id", "status", "strategy", "outcome", "spent")} if r else None,
            "queue": [{k: q.get(k) for k in ("who", "paper", "status")} for q in r.get("queue", [])],
            "strategies": [{"id": s["id"], "uses": s["uses"], "mean_gain": round(_mean(s), 2), "text": s["text"][:80]}
                           for s in a["strategies"]],
            "pending_approval": [c["id"] for c in a["candidates"].values() if c["status"] == "pending_approval"],
            "adoptions": a["adoptions"][-5:], "spent_tonight": round(round_spend(r["id"]), 4) if r else 0}


def _eval(who: str, paper: str, label: str) -> None:
    from .pipeline import reproduce
    tracing.init(label, "v2")
    try:
        print("EVAL", json.dumps(reproduce(paper, label, "v2"), default=str), flush=True)
    except ledger.BudgetExceeded as ex:
        print("BUDGET STOP:", ex, flush=True)
    finally:
        tracing.shutdown()


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "status"
    if cmd == "eval":
        _eval(*argv[1:4])
        return 0
    if cmd in ("start", "step"):
        tracing.init("evolve", "v2")
        try:
            print(json.dumps(start() if cmd == "start" else step(), indent=1, default=str))
        finally:
            tracing.shutdown()
        return 0
    if cmd == "approve":
        print(approve(argv[1]))
    elif cmd == "reject":
        print(reject(argv[1]))
    elif cmd == "pending":
        a = archive()
        for c in a["candidates"].values():
            if c["status"] == "pending_approval":
                print(f"{c['id']}  {c['delta']:+.2f} on the bench  {c['reason']}\n  file: {CANDS / c['id'] / 'builder_policy.py'}")
    else:
        print(json.dumps(status(), indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
