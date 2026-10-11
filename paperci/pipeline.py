"""Orchestrator. Stage outputs are persisted; a crashed run resumes where it stopped.

Triage and sealed claims are shared across runs ("shared" label) so that a
before/after comparison sees exactly the same targets and sealed values."""
from __future__ import annotations

import json
import os
import time
import traceback
from collections import Counter
from pathlib import Path

from . import db, ledger, live, sources, tracing
from .config import MODELS, ROBUST_RERUNS, RUNS_DIR, SOLVER_ATTEMPTS, STAGE_MAX_ATTEMPTS
from .sandbox import _docker, ensure_infra
from .stages import claims as claims_mod
from .stages import tasks, triage, verifier
from .stages.builder import Builder

SHARED = "shared"


def _load(arxiv_id: str, stage: str, label: str) -> dict | None:
    row = db.get_stage(arxiv_id, stage, label)
    if row and row["status"] == "done" and row["output"]:
        return json.loads(row["output"])
    return None


def _run_stage(arxiv_id: str, stage: str, label: str, fn, trace_id: str | None):
    """Persisted, resumable, bounded-retry stage execution."""
    cached = _load(arxiv_id, stage, label)
    if cached is not None:
        tracing.log("stage {stage} resumed from state", stage=stage)
        live.emit("resume", f"{STAGE_TITLES.get(stage, stage)} (already done, resumed from state)")
        return cached
    live.emit("start", STAGE_TITLES.get(stage, stage))
    t_stage = time.time()
    row = db.get_stage(arxiv_id, stage, label)
    attempts = (row["attempts"] if row else 0)
    last_exc = None
    while attempts < STAGE_MAX_ATTEMPTS:
        attempts += 1
        db.save_stage(arxiv_id, stage, label, status="running", attempts=attempts, trace_id=trace_id,
                      started_at=time.time())
        c0 = ledger.paper_total(arxiv_id)
        try:
            out = fn()
            db.save_stage(arxiv_id, stage, label, status="done", output=out, finished_at=time.time(),
                          cost_usd=ledger.paper_total(arxiv_id) - c0, error=None)
            live.emit("ok", f"{STAGE_TITLES.get(stage, stage)} done in {time.time() - t_stage:.0f}s"
                      f" (${ledger.paper_total(arxiv_id) - c0:.3f}){_stage_note(stage, out)}")
            return out
        except ledger.BudgetExceeded:
            db.save_stage(arxiv_id, stage, label, status="budget", finished_at=time.time())
            raise
        except Exception as e:
            last_exc = e
            tb = traceback.format_exc()[-3000:]
            db.save_stage(arxiv_id, stage, label, status="error", error=tb, finished_at=time.time())
            db.log_failure(arxiv_id, label, f"pipeline.{stage}", type(e).__name__, str(e)[:500], trace_id=trace_id)
            tracing.log("stage {stage} crashed ({err}); attempt {a}/{m}", level="error", stage=stage,
                        err=type(e).__name__, a=attempts, m=STAGE_MAX_ATTEMPTS)
            live.emit("fail", f"{STAGE_TITLES.get(stage, stage)} crashed: {type(e).__name__}; retry {attempts}/{STAGE_MAX_ATTEMPTS}")
            healed = ensure_infra() if "docker" in str(e).lower() or "Sandbox" in type(e).__name__ else []
            if healed:
                tracing.log("self-heal: {healed}", healed=healed)
                live.emit("heal", f"self-heal: {', '.join(healed)}")
    raise RuntimeError(f"stage {stage} failed after {attempts} attempts: {last_exc}")


STAGE_TITLES = {
    "snapshot": "Snapshot repository metadata", "triage": "Triage: can this run on a CPU sandbox?",
    "claims": "Extract and seal the paper's headline claims", "build": "Builder agent: get the code running",
    "verify": "Independent verifier: offline run (network off)", "robustness": "Robustness: reruns with other seeds",
    "verdict": "Verdict: claims vs measured, integrity checks", "package": "Package as an executable task",
    "oracle": "Oracle: replay the solution in a fresh environment", "trainability": "Trainability: can a cheap solver do it?",
}


def _stage_note(stage: str, out: dict) -> str:
    try:
        if stage == "triage":
            return f" -> {out['decision']}" + (f" ({'; '.join(out.get('rule_reasons') or [])[:120]})"
                                               if out["decision"] != "accept" else "")
        if stage == "claims":
            return f" -> {out['n_claims']} claims sealed (sha256 {out['sealed_sha256'][:12]}...)"
        if stage == "build":
            return f" -> {out['status']} in {out.get('turns')} turns" + (f", scale {out['scale']}" if out.get("scale") else "")
        if stage in ("verify", "robustness"):
            return " -> exit codes " + str([r["exit_code"] for r in out.get("runs", [])])
        if stage == "verdict":
            return f" -> {out['verdict']} ({out['confidence']})" + (f", flags {out['integrity']['flags']}" if out["integrity"]["flags"] else "")
        if stage == "oracle":
            return f" -> reward {out['reward']}"
        if stage == "trainability":
            return f" -> {out['label']}"
    except Exception:
        pass
    return ""


def paper_row(arxiv_id: str) -> dict:
    r = db.q1("SELECT * FROM papers WHERE arxiv_id=?", (arxiv_id,))
    if not r:
        raise KeyError(arxiv_id)
    return dict(r)


def prepare(arxiv_id: str) -> dict:
    """Shared front half: snapshot -> triage -> sealed claims (no sandbox)."""
    paper = paper_row(arxiv_id)
    with tracing.span(f"paper-prepare:{arxiv_id}", kind="WORKFLOW", arxiv_id=arxiv_id, split=paper["split"]) as root:
        tid = tracing.trace_id_of(root)

        def snap():
            s = sources.repo_snapshot(paper["repo_url"])
            s["tree"] = s.get("tree", [])[:4000]
            s["readme"] = s.get("readme", "")[:30000]
            return s
        snapshot = _run_stage(arxiv_id, "snapshot", SHARED, snap, tid)
        tri = _run_stage(arxiv_id, "triage", SHARED, lambda: triage.triage(arxiv_id, SHARED, paper, snapshot), tid)
        out = {"triage": tri["decision"], "trace_id": tid}
        if tri["decision"] == "accept":
            cl = _run_stage(arxiv_id, "claims", SHARED, lambda: claims_mod.extract(arxiv_id, SHARED, paper), tid)
            out["n_claims"] = cl["n_claims"]
        db.set_paper(arxiv_id, status="prepared")
        tracing.set_attr(root, "triage", out["triage"])
        return out


def reproduce(arxiv_id: str, label: str, variant: str, hints: str = "", focus: list[str] | None = None) -> dict:
    """Back half: build -> verify -> robustness -> verdict -> package -> oracle -> trainability."""
    paper = paper_row(arxiv_id)
    snapshot = _load(arxiv_id, "snapshot", SHARED)
    cl = _load(arxiv_id, "claims", SHARED)
    if not snapshot or not cl or not cl["n_claims"]:
        return {"skipped": "not prepared or no verifiable claims"}
    rdir = RUNS_DIR / arxiv_id.replace("/", "_") / label
    with tracing.span(f"paper:{arxiv_id}", kind="WORKFLOW", arxiv_id=arxiv_id, run_label=label,
                      variant=variant, split=paper["split"], builder_model=MODELS["builder"]) as root:
        tid = tracing.trace_id_of(root)
        # Evaluation runs of candidate builder versions: build + one offline verification only, and they never
        # touch the paper's public verdict or status.
        light = os.environ.get("PAPERCI_EVAL_LIGHT") == "1"
        tracing.set_attr(root, "eval_light", light)
        if not light:
            db.set_paper(arxiv_id, trace_id=tid, status=f"running:{label}")
        healed = ensure_infra()
        if healed:
            tracing.log("self-heal before run: {healed}", healed=healed)
        sealed = claims_mod.unseal(arxiv_id, "verifier")
        sealed_values = [c.value for c in sealed]
        sealed_sha = db.q1("SELECT sha256 FROM sealed_claims WHERE arxiv_id=?", (arxiv_id,))["sha256"]

        build = _run_stage(arxiv_id, "build", label, lambda: Builder(
            arxiv_id, label, paper, snapshot, cl["targets"], sealed_values, variant, hints, focus).run(), tid)

        def verify():
            if build.get("status") != "built":
                return {"runs": [], "per_claim": {}}
            with tracing.span("stage:verifier", kind="CHAIN", model=MODELS["verifier"]) as vs:
                runs = [verifier.offline_run(arxiv_id, label, build["image"], 0)]
                per_claim = {}
                if runs[0]["exit_code"] == 0:
                    arts = verifier.read_artifacts(runs[0]["artifact_dir"])
                    metric_counts = Counter(c.metric.strip().lower() for c in sealed)
                    for c in sealed:
                        ext = verifier.extract(arxiv_id, label, c, arts,
                                               shared_metric=metric_counts[c.metric.strip().lower()] > 1)
                        per_claim[c.claim_id] = {"metric": c.metric, "extraction": ext,
                                                 "raw_values": [ext["value"]] if ext.get("found") else []}
                tracing.set_attr(vs, "metrics_found", sum(1 for v in per_claim.values() if v["raw_values"]))
                return {"runs": runs, "per_claim": per_claim}
        ver = _run_stage(arxiv_id, "verify", label, verify, tid)

        def robustness():
            runs, per_claim = list(ver["runs"]), json.loads(json.dumps(ver["per_claim"]))
            if light or not runs or runs[0]["exit_code"] != 0 or runs[0]["seconds"] > 420 or not any(v["raw_values"] for v in per_claim.values()):
                return {"runs": runs, "per_claim": per_claim, "reruns": 0}
            with tracing.span("stage:robustness", kind="CHAIN", reruns=ROBUST_RERUNS):
                for seed in range(1, ROBUST_RERUNS + 1):
                    r = verifier.offline_run(arxiv_id, label, build["image"], seed)
                    runs.append(r)
                    if r["exit_code"] != 0:
                        continue
                    arts = verifier.read_artifacts(r["artifact_dir"])
                    for c in sealed:
                        pc = per_claim.get(c.claim_id)
                        if not pc or not pc["raw_values"]:
                            continue
                        spec = tasks.extraction_spec(c.claim_id, pc["extraction"], pc["raw_values"][0])
                        val = _apply_spec(spec, arts) if spec else None
                        if val is not None:
                            pc["raw_values"].append(val)
            return {"runs": runs, "per_claim": per_claim, "reruns": ROBUST_RERUNS}
        rob = _run_stage(arxiv_id, "robustness", label, robustness, tid)

        def decide():
            per_claim = rob["per_claim"]
            for c in sealed:
                pc = per_claim.get(c.claim_id)
                if pc and pc["raw_values"]:
                    norm_vals, how = [], "none"
                    for v in pc["raw_values"]:
                        nv, how = verifier.normalize(c, v)
                        norm_vals.append(nv)
                    pc["normalization"] = how
                    pc["comparison"] = verifier.compare(c, norm_vals)
            integ = verifier.integrity(rdir / "build", sealed, rob["runs"], build.get("egress", {}), tree=snapshot.get("tree", []))
            v, conf, reason = verifier.verdict(build, rob["runs"], per_claim, integ)
            with tracing.span("stage:verdict", kind="GUARDRAIL", verdict=v, confidence=conf) as s:
                tracing.set_attr(s, "reason", reason)
                tracing.set_attr(s, "integrity_flags", integ["flags"])
                tracing.guardrail(s, {"integrity": integ["flags"], "egress_denied": integ["egress_denied"]},
                                  not integ["flags"], f"{v} ({conf}): {reason}")
            return {"verdict": v, "confidence": conf, "reason": reason, "integrity": integ, "per_claim": per_claim}
        dec = _run_stage(arxiv_id, "verdict", label, decide, tid)
        if not light:
            db.set_paper(arxiv_id, verdict=dec["verdict"], confidence=dec["confidence"], status=f"done:{label}")
        _write_report(arxiv_id, label, paper, snapshot, build, rob, dec, tid, sealed_sha)

        pkg = None
        skip_pkg = os.environ.get("PAPERCI_SKIP_PACKAGE") == "1"  # external evals only need the verdict
        if not light and not skip_pkg and dec["verdict"] != "blocked-infra" and not dec["integrity"]["flags"]:
            pkg = _run_stage(arxiv_id, "package", label, lambda: tasks.package(
                arxiv_id, label, paper, snapshot, build, cl, dec["per_claim"], dec["verdict"],
                dec["confidence"], tid, sealed_sha) or {"task_id": None}, tid)
        if pkg and pkg.get("task_id"):
            tdir = Path(pkg["path"])
            orc = _run_stage(arxiv_id, "oracle", label, lambda: tasks.oracle_check(tdir, snapshot), tid)
            if orc["reward"] < 0.999:
                tasks.reject(tdir, f"oracle_reward={orc['reward']}")
                db.save_stage(arxiv_id, "package", label, output={**pkg, "task_id": None, "was": pkg["task_id"],
                                                                 "invalidated": ["oracle_failed"]})
            else:
                def train():
                    atts = [tasks.solver_attempt(arxiv_id, label, tdir, snapshot, i) for i in range(1, SOLVER_ATTEMPTS + 1)]
                    return {"attempts": atts, "label": tasks.trainability(atts)}
                tr = _run_stage(arxiv_id, "trainability", label, train, tid)
                meta = json.loads((tdir / "paperci.json").read_text())
                meta.update(oracle_reward=orc["reward"], trainability=tr["label"],
                            solver_pass_rate=sum(a["reward"] >= 0.999 for a in tr["attempts"]) / max(len(tr["attempts"]), 1))
                (tdir / "paperci.json").write_text(json.dumps(meta, indent=1))
        cost = ledger.paper_total(arxiv_id, label) + sum(
            ledger.paper_total(arxiv_id, f"{label}-solver{i}") for i in range(1, SOLVER_ATTEMPTS + 1))
        if build.get("image") and not os.environ.get("PAPERCI_KEEP_IMAGES"):
            # Built images are only needed for the offline reruns above (the oracle replays from the base
            # image); one can be >15 GB, and keeping them filled the host disk once.
            _docker("rmi", "-f", build["image"], check=False)
        tracing.set_attr(root, "verdict", dec["verdict"])
        tracing.set_attr(root, "cost_usd", round(cost, 4))
        tracing.set_attr(root, "build_status", build.get("status"))
        return {"verdict": dec["verdict"], "confidence": dec["confidence"], "build": build.get("status"),
                "cost_usd": round(cost, 4), "trace_id": tid, "task": (pkg or {}).get("task_id")}


def _apply_spec(spec: dict, arts: dict[str, str]) -> float | None:
    text = arts.get(spec["file"])
    if text is None:
        return None
    if spec["type"] == "json":
        try:
            d = json.loads(text)
            for k in spec["key"].split("."):
                d = d[int(k)] if isinstance(d, list) else d[k]
            return float(d)
        except Exception:
            return None
    return tasks.apply_regex(spec, text)


def _write_report(arxiv_id, label, paper, snap, build, rob, dec, tid, sealed_sha) -> None:
    lines = [f"# PaperCI reproducibility report: arXiv:{arxiv_id} ({label})", "",
             f"- Paper: {paper['title']}", f"- Repo: {snap.get('full_name')} @ {snap.get('sha')}",
             f"- Verdict: **{dec['verdict']}** (confidence {dec['confidence']})", f"- Reason: {dec['reason']}",
             f"- Builder: status={build.get('status')} turns={build.get('turns')} scale={build.get('scale')} "
             f"variant={build.get('variant')} cost=${build.get('cost_usd')}",
             f"- Scale notes: {build.get('scale_notes', '-')}",
             f"- Offline runs: {[(r['seed'], r['exit_code'], r['seconds']) for r in rob['runs']]}",
             f"- Sealed claims sha256: `{sealed_sha}`", f"- neatlogs trace: `{tid}`",
             f"- Integrity: {dec['integrity']['flags'] or 'clean'}; egress denied: {dec['integrity']['egress_denied'] or 'none'}",
             "", "| claim | metric | claimed | measured (mean±sd, n) | tolerance | within | evidence |", "|---|---|---|---|---|---|---|"]
    for cid, pc in dec["per_claim"].items():
        cmp_ = pc.get("comparison")
        if cmp_:
            lines.append(f"| {cid} | {pc['metric']} | {cmp_['claimed']} | {cmp_['measured_mean']:.4g}±{cmp_['measured_sd']:.2g} (n={cmp_['n']}) "
                         f"| {cmp_['tolerance']:.3g} | {cmp_['within']} | `{pc['extraction'].get('evidence', '')[:80]}` ({pc['extraction']['method']}) |")
        else:
            lines.append(f"| {cid} | {pc['metric']} | - | not found | - | - | {pc['extraction'].get('method')} |")
    lines += ["", "Failures seen during build (class -> recovered):"]
    lines += [f"- {f['phase']}: {f['category']} -> {'recovered' if f.get('recovered') else 'not recovered'}" for f in build.get("failures", [])] or ["- none"]
    p = RUNS_DIR / arxiv_id.replace("/", "_") / label / "report.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
