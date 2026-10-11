"""External eval: PaperCI's blind verdicts vs the Hugging Face ICML 2026 Open Reproductions consensus.

Hugging Face's community (1,221 people, coding agents, GPUs) reproduced 2,226 ICML 2026 papers claim by claim;
a GLM-5.2 judge froze 35,908 claim verdicts in ICML-2026-agent-repro/verdicts. We take papers where at least
two medium/high-quality logbooks agree (>= 75%) on "verified" or "falsified", reproduce them blind (their
verdicts are never shown to any agent), score agreement, and send every disagreement to a human reviewer.
Human decisions become gold labels, so the eval improves every time it runs.

Those labels are silver: an LLM judge read agent logbooks. Only the human review turns them into gold.
Verdicts about named papers stay private (review page behind an invite code); public output is aggregate.

    python -m paperci.icml_eval fetch | screen [n] | queue [n] | step | score | status
"""
from __future__ import annotations

import collections
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

from . import db, ledger, ops, tracing
from .config import DATA_DIR, ROOT

ICML = DATA_DIR / "icml"
CANDS = ICML / "candidates.json"
STATE = ICML / "state.json"
LABELS = ICML / "labels.jsonl"
BENCH_FILE = ICML / "bench.json"
HF = "https://huggingface.co/datasets/ICML-2026-agent-repro"
SPLIT = "icml"
BENCH_N = int(os.getenv("PAPERCI_ICML_BENCH_N", "2"))
RUN_TIMEOUT = 100 * 60
OURS = {"verified": "verified", "falsified-candidate": "falsified", "toy-scale": "toy"}  # anything else: no decision


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


def _get(name: str, repo: str) -> dict:
    path = ICML / f"hf-{repo}-{name}"
    if not path.exists():
        r = requests.get(f"{HF}/{repo}/resolve/main/{name}", timeout=300)
        r.raise_for_status()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(r.content)
    return json.loads(path.read_text(encoding="utf-8"))


def _logbook_label(lb: dict) -> str | None:
    vs = [c["verdict"] for c in lb.get("claims", [])]
    if not vs:
        return None
    if "falsified" in vs:
        return "falsified"
    if all(v == "verified" for v in vs):
        return "verified"
    return "toy" if "toy" in vs and "verified" not in vs else "mixed"


# ---------------------------------------------------------------- 1. fetch + consensus
def fetch() -> dict:
    papers = {p["orid"]: p for p in _get("papers.json", "challenge")["papers"]}
    by: dict[str, list[dict]] = collections.defaultdict(list)
    for lb in _get("verdicts_final.json", "verdicts").values():
        by[lb.get("orid")].append(lb)
    old = {c["arxiv"]: c for c in _load(CANDS, [])}
    out = []
    for orid, lbs in by.items():
        p = papers.get(orid)
        good = [lb for lb in lbs if lb.get("quality") in ("high", "medium")]
        labs = [x for x in (_logbook_label(lb) for lb in good) if x]
        if not p or not p.get("arxiv") or len(labs) < 2:
            continue
        top, n = collections.Counter(labs).most_common(1)[0]
        if top not in ("verified", "falsified") or n / len(labs) < 0.75:
            continue
        claims: dict[str, dict] = {}
        for lb in good:
            for c in lb.get("claims", []):
                k = " ".join(c["claim"].split())[:400]
                e = claims.setdefault(k, {"claim": k, "verdicts": collections.Counter(), "evidence": []})
                e["verdicts"][c["verdict"]] += 1
                if len(e["evidence"]) < 2 and c.get("evidence"):
                    e["evidence"].append({"verdict": c["verdict"], "text": c["evidence"][:600], "space": lb["space_id"]})
        prev = old.get(p["arxiv"], {})
        out.append({"arxiv": p["arxiv"], "orid": orid, "title": p["title"], "area": p.get("area"),
                    "label": top, "consensus_share": round(n / len(labs), 2), "n_logbooks": len(labs),
                    "logbooks": [lb["space_id"] for lb in good][:12],
                    "claims": [dict(c, verdicts=dict(c["verdicts"])) for c in claims.values()][:12],
                    "screen": prev.get("screen"), "role": prev.get("role")})
    # Deterministic order (hash of the id), so screening order is not chosen by looking at results.
    out.sort(key=lambda c: hashlib.sha256(c["arxiv"].encode()).hexdigest())
    _save(CANDS, out)
    n = collections.Counter(c["label"] for c in out)
    return {"candidates": len(out), **n}


# ---------------------------------------------------------------- 2. screen (fetch code, triage, seal claims)
def screen(n: int = 40) -> dict:
    from . import sources
    from .pipeline import prepare
    cands = _load(CANDS, [])
    done = 0
    for c in cands:
        if done >= n:
            break
        if c.get("screen"):
            continue
        done += 1
        try:
            row = db.q1("SELECT split FROM papers WHERE arxiv_id=?", (c["arxiv"],))
            if row and row["split"] != SPLIT:
                c["screen"] = {"status": "skip", "why": f"already in PaperCI as split {row['split']}"}
                continue
            if not row:
                p = sources.fetch_paper(c["arxiv"])
                if not p["repo_url"]:
                    c["screen"] = {"status": "no_code"}
                    continue
                p["split"] = SPLIT
                db.upsert_paper(p)
            prep = prepare(c["arxiv"])
            ok = prep.get("triage") == "accept" and (prep.get("n_claims") or 0) > 0
            c["screen"] = {"status": "runnable" if ok else "not_runnable", "triage": prep.get("triage"),
                           "n_claims": prep.get("n_claims")}
        except ledger.BudgetExceeded as ex:
            c["screen"] = None
            _save(CANDS, cands)
            return {"status": "budget", "why": str(ex)}
        except Exception as ex:
            c["screen"] = {"status": "error", "why": f"{type(ex).__name__}: {str(ex)[:160]}"}
        _save(CANDS, cands)
    _assign_roles(cands)
    _save(CANDS, cands)
    return dict(collections.Counter((c.get("screen") or {}).get("status", "unscreened") for c in cands))


def _assign_roles(cands: list[dict]) -> None:
    """The first BENCH_N runnable *verified* papers become the evolution benchmark; every other runnable paper is
    eval-only. Bench papers tune the agent, so they never count in the eval score."""
    bench = [c for c in cands if c.get("role") == "bench"]
    for c in cands:
        if (c.get("screen") or {}).get("status") != "runnable" or c.get("role"):
            continue
        if len(bench) < BENCH_N and c["label"] == "verified":
            c["role"] = "bench"
            bench.append(c)
        else:
            c["role"] = "eval"
    _save(BENCH_FILE, [c["arxiv"] for c in bench])


# ---------------------------------------------------------------- 3. blind runs (stepwise, preemptible)
def queue(n: int = 10) -> dict:
    st = _load(STATE, {"runs": []})
    have = {r["arxiv"] for r in st["runs"]}
    add = [c for c in _load(CANDS, []) if c.get("role") == "eval" and c["arxiv"] not in have][:n]
    st["runs"] += [{"arxiv": c["arxiv"], "status": "pending", "attempts": 0, "queued": time.time()} for c in add]
    _save(STATE, st)
    ops.event("cycle", f"ICML external eval: {len(add)} blind reproduction(s) queued (Hugging Face consensus hidden "
                       "from every agent)")
    return {"queued": len(add), "total": len(st["runs"])}


def has_pending() -> bool:
    return any(r["status"] == "pending" for r in _load(STATE, {"runs": []})["runs"])


def run_one(arxiv: str, label: str) -> None:
    from .pipeline import reproduce
    tracing.init(label, "v2")
    try:
        print("ICML", json.dumps(reproduce(arxiv, label, "v2"), default=str), flush=True)
    except ledger.BudgetExceeded as ex:
        print("BUDGET STOP:", ex, flush=True)
    finally:
        tracing.shutdown()


def step() -> dict:
    """One blind run per worker slot; ops runs this preemptibly, so an author's submission stops it."""
    st = _load(STATE, {"runs": []})
    item = next((r for r in st["runs"] if r["status"] == "pending"), None)
    if not item:
        return {"status": "idle"}
    item["attempts"] += 1
    if item["attempts"] > 2:
        item["status"] = "failed"
        _save(STATE, st)
        return {"status": "failed twice", "arxiv": item["arxiv"]}
    label = f"icml-{item['arxiv']}" + (f"-r{item['attempts'] - 1}" if item["attempts"] > 1 else "")
    item.update(label=label, started=time.time())
    _save(STATE, st)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PAPERCI_SKIP_PACKAGE": "1", "PAPERCI_BUDGETED_JOB": "1",
           "PAPERCI_END_USER": "icml-eval", "PAPERCI_SESSION": "run:icml-eval"}
    p = subprocess.run([sys.executable, "-u", "-m", "paperci.icml_eval", "run", item["arxiv"], label], cwd=ROOT,
                       capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=RUN_TIMEOUT)
    (ops.OPS / f"icml-{item['arxiv']}.log").write_text(p.stdout + p.stderr, encoding="utf-8")
    st = _load(STATE, st)
    for r in st["runs"]:
        if r["arxiv"] == item["arxiv"]:
            res = ours(item["arxiv"], label)
            r.update(status="done" if res else ("pending" if r["attempts"] < 2 else "failed"), result=res,
                     finished=time.time())
            if res:
                try:
                    from . import review
                    r["review"] = str(review.write(item["arxiv"], label))
                except Exception:
                    pass
    _save(STATE, st)
    return {"status": "ran", "arxiv": item["arxiv"]}


def release() -> None:
    """A run interrupted for an author is redone later and does not count as an attempt."""
    st = _load(STATE, {"runs": []})
    for r in st["runs"]:
        if r["status"] == "pending" and r.get("started") and not r.get("result"):
            r["attempts"] = max(0, r["attempts"] - 1)
            r.pop("started", None)
    _save(STATE, st)


def ours(arxiv: str, label: str) -> dict | None:
    v = db.q1("SELECT output FROM stages WHERE arxiv_id=? AND run_label=? AND stage='verdict' AND status='done'", (arxiv, label))
    b = db.q1("SELECT output FROM stages WHERE arxiv_id=? AND run_label=? AND stage='build' AND status='done'", (arxiv, label))
    if not b:
        return None
    build = json.loads(b["output"])
    dec = json.loads(v["output"]) if v else {"verdict": "blocked-infra", "reason": build.get("reason", "")}
    claims = []
    for cid, pc in (dec.get("per_claim") or {}).items():
        cmp_ = pc.get("comparison") or {}
        claims.append({"id": cid, "metric": pc.get("metric"), "claimed": cmp_.get("claimed"),
                       "measured": cmp_.get("measured_mean"), "n": cmp_.get("n"), "within": cmp_.get("within")})
    return {"verdict": dec.get("verdict"), "reason": (dec.get("reason") or "")[:500], "build": build.get("status"),
            "give_up": build.get("give_up_category"), "turns": build.get("turns"), "claims": claims}


# ---------------------------------------------------------------- 4. score + human labels
def labels() -> dict[str, dict]:
    out: dict[str, dict] = {}
    if LABELS.exists():
        for line in LABELS.read_text(encoding="utf-8").splitlines():
            try:
                x = json.loads(line)
                out[x["arxiv"]] = x  # latest decision wins
            except Exception:
                continue
    return out


def add_label(arxiv: str, choice: str, note: str, reviewer: str) -> None:
    ICML.mkdir(parents=True, exist_ok=True)
    with open(LABELS, "a", encoding="utf-8") as f:
        f.write(json.dumps({"arxiv": arxiv, "choice": choice, "note": note[:2000], "reviewer": reviewer,
                            "ts": time.time()}) + "\n")


def items() -> list[dict]:
    cands = {c["arxiv"]: c for c in _load(CANDS, [])}
    hl = labels()
    out = []
    for r in _load(STATE, {"runs": []})["runs"]:
        c = cands.get(r["arxiv"])
        if not c or not r.get("result"):
            continue
        mine = OURS.get(r["result"]["verdict"])
        out.append({**c, "run": r, "ours": mine, "decided": mine is not None,
                    "agree": mine == c["label"] if mine else None, "human": hl.get(r["arxiv"])})
    return out


def score() -> dict:
    xs = items()
    dec = [x for x in xs if x["decided"]]
    conf = collections.Counter(f"HF {x['label']} / PaperCI {x['ours'] or 'no decision'}" for x in xs)
    human = [x for x in xs if x["human"]]
    s = {"runs": len(xs), "decided": len(dec), "coverage": round(len(dec) / len(xs), 2) if xs else None,
         "agree": sum(1 for x in dec if x["agree"]), "agreement": round(sum(1 for x in dec if x["agree"]) / len(dec), 2) if dec else None,
         "confusion": dict(conf), "human_reviewed": len(human),
         "human_says_paperci_right": sum(1 for x in human if x["human"]["choice"] == "paperci"),
         "human_says_hf_right": sum(1 for x in human if x["human"]["choice"] == "hf"),
         "needs_review": sum(1 for x in xs if not x["human"] and (x["agree"] is False or not x["decided"]))}
    _save(ICML / "score.json", s)
    return s


def status() -> dict:
    st = _load(STATE, {"runs": []})
    return {"candidates": dict(collections.Counter((c.get("screen") or {}).get("status", "unscreened") for c in _load(CANDS, []))),
            "bench": _load(BENCH_FILE, []),
            "runs": dict(collections.Counter(r["status"] for r in st["runs"])), "score": score()}


# ---------------------------------------------------------------- 5. daily golden check
def golden_enqueue() -> dict:
    """Re-run one paper where PaperCI and the HF consensus agreed; a different verdict tomorrow is a regression."""
    agreed = [x for x in items() if x["agree"]]
    if not agreed:
        return {"status": "no agreed paper yet"}
    g = sorted(agreed, key=lambda x: x["arxiv"])[int(time.time() // 86400) % len(agreed)]
    st = _load(STATE, {"runs": []})
    day = time.strftime("%Y%m%d", ops._local())
    st.setdefault("golden", [])
    if any(x["day"] == day for x in st["golden"]):
        return {"status": "already queued today"}
    st["golden"].append({"day": day, "arxiv": g["arxiv"], "expect": g["ours"], "status": "pending", "attempts": 0})
    _save(STATE, st)
    return {"status": "queued", "arxiv": g["arxiv"]}


def golden_pending() -> bool:
    return any(x["status"] == "pending" for x in _load(STATE, {}).get("golden", []))


def golden_step() -> dict:
    st = _load(STATE, {"runs": []})
    g = next((x for x in st.get("golden", []) if x["status"] == "pending"), None)
    if not g:
        return {"status": "idle"}
    g["attempts"] += 1
    if g["attempts"] > 2:
        g["status"] = "failed"
        _save(STATE, st)
        ops.event("incident", "Daily golden check could not finish twice; check the pipeline", healed=False, component="eval")
        return {"status": "failed"}
    label = f"icml-golden-{g['day']}" + (f"-r{g['attempts'] - 1}" if g["attempts"] > 1 else "")
    _save(STATE, st)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PAPERCI_SKIP_PACKAGE": "1", "PAPERCI_BUDGETED_JOB": "1",
           "PAPERCI_END_USER": "golden-check", "PAPERCI_SESSION": "run:golden"}
    subprocess.run([sys.executable, "-u", "-m", "paperci.icml_eval", "run", g["arxiv"], label], cwd=ROOT,
                   capture_output=True, text=True, env=env, timeout=RUN_TIMEOUT)
    res = ours(g["arxiv"], label)
    st = _load(STATE, st)
    for x in st["golden"]:
        if x["day"] == g["day"]:
            got, want = OURS.get((res or {}).get("verdict")), x["expect"]
            x.update(status="done" if res else "pending", got=got, ok=got == want)
            if res:
                what = "unchanged" if got == want else f"CHANGED ({want} -> {got})"
                ops.event("chaos" if got == want else "incident",
                          f"Daily golden check: a known paper was re-verified end to end, verdict {what}",
                          recovered=got == want, healed=got == want, component="eval")
    _save(STATE, st)
    return {"status": "ran"}


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "status"
    if cmd == "run":
        run_one(argv[1], argv[2])
        return 0
    tracing.init("icml-eval", "v2")
    try:
        os.environ["PAPERCI_BUDGETED_JOB"] = "1"
        fn = {"fetch": fetch, "screen": lambda: screen(int(argv[1]) if len(argv) > 1 else 40),
              "queue": lambda: queue(int(argv[1]) if len(argv) > 1 else 10), "step": step, "score": score,
              "status": status, "golden": golden_enqueue, "golden-step": golden_step}[cmd]
        print(json.dumps(fn(), indent=1, default=str))
    finally:
        tracing.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
