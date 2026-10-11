"""CodeRabbit-style review of one paper reproduction: a walkthrough, claims-vs-measured table, CI-style
checks, actionable suggestions for the authors, and "reproduce it yourself" steps.

Everything is derived from stored stage outputs (no LLM call), so a review can be re-rendered at any time.
Reviews name the paper and are written to registry/reviews/ (git-ignored, private); `anonymize=True`
removes identity (title, repo, quotes) for public demos."""
from __future__ import annotations

import hashlib
import html
import json
import re
import time
from pathlib import Path

from . import db, ledger
from .config import MODELS, REGISTRY_DIR, RUNS_DIR
from .stages import claims as claims_mod

REVIEWS = REGISTRY_DIR / "reviews"

STATUS = {  # key -> (label, css class, short description)
    "reproduced": ("Reproduced", "ok", "within calibrated tolerance in every offline rerun"),
    "reproduced_reduced": ("Reproduced at reduced scale", "ok", "within tolerance, but the run was scaled down"),
    "match_1": ("Matches (1 run)", "warn", "within tolerance, only one offline run produced it"),
    "differs": ("Not reproduced", "bad", "outside tolerance in every offline rerun"),
    "differs_reduced": ("Differs at reduced scale", "warn", "outside tolerance; expected when the run is scaled down"),
    "differs_1": ("Differs (1 run)", "warn", "outside tolerance, single run"),
    "not_measured": ("Not measured", "na", "no run printed this metric"),
    "blocked": ("Blocked", "na", "the code could not be run in the sandbox"),
    "untrusted": ("Run not trusted", "bad", "integrity check failed; numbers are not used"),
}

INTEGRITY_TEXT = {  # flag -> (name, passed, failed)
    "hardcoded_claim_value": ("No hardcoded answers", "no sealed claim value appears in anything the builder wrote",
                              "a sealed claim value appears in code or commands the builder wrote"),
    "prebuilt_result_files": ("No copied results", "no output file is a byte-identical copy of a result file in the repo",
                              "output files are copies of result files shipped in the repo"),
    "precomputed_results_replay": ("Computed, not replayed", "the run computes its numbers instead of loading stored results",
                                   "the run replays stored results instead of computing them"),
    "no_repo_code_path": ("Uses the paper's code", "the run script executes the repository's own code",
                          "the run script never executes the repository's own code"),
}

GIVE_UP_ADVICE = {
    "data_unavailable": "Release the evaluation data (or a download script pointing at a stable host such as Zenodo, "
                        "Hugging Face or a GitHub release). PaperCI's agent concluded: \"{reason}\"",
    "needs_credentials": "The code path needs credentials (hosted LLM / API keys). Provide a cached-response or "
                         "offline mode so the numbers can be re-derived without keys.",
    "gpu_required": "Add a CPU fallback (or a tiny config) for the headline experiment; the code assumes a GPU.",
    "too_large": "Provide a scaled-down config with expected numbers so the result can be checked in minutes.",
}

FAILURE_ADVICE = {
    "missing_module": "Imports failed for a module that is not declared as a dependency; add it to requirements.",
    "api_incompat": "The code breaks with current library versions; pin the versions you used (pip freeze).",
    "cuda_required": "Some code calls CUDA unconditionally; guard with `torch.cuda.is_available()`.",
    "data_missing": "A file the code expects is not in the repo and no download step creates it.",
    "egress_denied": "The code downloads from a host that a sandboxed reviewer cannot reach; mirror the data.",
}


def anon_id(aid: str) -> str:
    return "P-" + hashlib.sha256(aid.encode()).hexdigest()[:6]


def _load(aid: str, stage: str, label: str) -> dict | None:
    r = db.get_stage(aid, stage, label)
    return json.loads(r["output"]) if r and r["status"] == "done" and r["output"] else None


def _claim_status(pc: dict | None, built: bool, reduced: bool, flagged: bool) -> str:
    if not built:
        return "blocked"
    if flagged:
        return "untrusted"
    cmp_ = (pc or {}).get("comparison")
    if not cmp_:
        return "not_measured"
    if cmp_["within"]:
        if cmp_["n"] < 2:
            return "match_1"
        return "reproduced_reduced" if reduced else "reproduced"
    if cmp_["n"] < 2:
        return "differs_1"
    return "differs_reduced" if reduced else "differs"


PATCH_EXT = re.compile(r"\.(py|txt|cfg|toml|ini|ya?ml|sh|json|ipynb|md|r|jl|m|c|cc|cpp|h)$|(^|/)(requirements[^/]*|setup\.py|Makefile|Dockerfile)$", re.I)
GENERATED_DIR = re.compile(r"(^|/)(out|outputs?|results?|logs?|runs?|checkpoints?|ckpts?|wandb|mlruns|data|datasets|cache|"
                           r"__pycache__|\.ipynb_checkpoints|lightning_logs|figures?|plots?|build|dist|[^/]+\.egg-info)/", re.I)


def reproduce_file(script: str) -> dict | None:
    """The builder's verified run script as a new repo file, with sandbox paths made repo-relative."""
    s = (script or "").strip()
    if not s or len(s) > 8000:
        return None
    s = s.replace("/work/repo/", "./").replace("/work/repo", ".").replace("/work/out", "./paperci_out")
    if not s.startswith("#!"):
        s = "#!/usr/bin/env bash\n" + s
    s = s.replace("#!/usr/bin/env bash", "#!/usr/bin/env bash\n# Generated and verified by PaperCI: regenerates the paper's headline numbers.", 1)
    lines = s.splitlines()
    body = f"--- /dev/null\n+++ b/reproduce.sh\n@@ -0,0 +1,{len(lines)} @@\n" + "\n".join("+" + l for l in lines) + "\n"
    return {"path": "reproduce.sh", "new": True, "added": len(lines), "removed": 0,
            "diff": "diff --git a/reproduce.sh b/reproduce.sh\nnew file mode 100755\n" + body}


def verified_patch(diff: str) -> list[dict]:
    """Split the builder's `diff -ruN orig work` into per-file source changes, dropping binaries, generated
    outputs and huge new files: what is left is the change the author would commit."""
    files = []
    for sec in re.split(r"(?m)^diff -ruN ", diff or "")[1:]:
        head, _, body = sec.partition("\n")
        m = re.search(r"/work/repo/(\S+)$", head)
        if not m or sec.startswith("Binary") or "\nBinary files" in sec[:400]:
            continue
        path = m.group(1)
        if not PATCH_EXT.search(path) or GENERATED_DIR.search(path):
            continue
        new = "\n--- /dev/null" in "\n" + body[:300] or re.search(r"(?m)^--- /opt/orig/repo/\S+\s+1970-", body[:300])
        added = sum(1 for l in body.splitlines() if l.startswith("+") and not l.startswith("+++"))
        removed = sum(1 for l in body.splitlines() if l.startswith("-") and not l.startswith("---"))
        if (new and added > 300) or added + removed > 600 or added + removed == 0:
            continue
        body = re.sub(r"(?m)^--- /opt/orig/repo/(\S+).*$", r"--- a/\1", body)
        body = re.sub(r"(?m)^\+\+\+ /work/repo/(\S+).*$", r"+++ b/\1", body)
        files.append({"path": path, "new": bool(new), "added": added, "removed": removed,
                      "diff": f"diff --git a/{path} b/{path}\n" + body.rstrip("\n") + "\n"})
    return files


def _fmt(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.4g}"
    return str(v)


def build_review(aid: str, label: str, anonymize: bool = False) -> dict:
    paper = dict(db.q1("SELECT * FROM papers WHERE arxiv_id=?", (aid,)))
    snap = _load(aid, "snapshot", "shared") or {}
    tri = _load(aid, "triage", "shared") or {}
    cl = _load(aid, "claims", "shared") or {}
    build = _load(aid, "build", label)
    rob = _load(aid, "robustness", label) or {}
    dec = _load(aid, "verdict", label) or {}
    pkg = _load(aid, "package", label) or {}
    orc = _load(aid, "oracle", label)
    trn = _load(aid, "trainability", label)
    sealed = {c.claim_id: c for c in claims_mod.unseal(aid, "report")} if cl.get("n_claims") else {}
    rdir = RUNS_DIR / aid.replace("/", "_") / label / "build"

    built = bool(build) and build.get("status") == "built"
    reduced = bool(build) and build.get("scale") == "reduced"
    flags = (dec.get("integrity") or {}).get("flags", [])
    per = dec.get("per_claim", {})
    claims = []
    for t in cl.get("targets", []):
        c = sealed.get(t["claim_id"])
        pc = per.get(t["claim_id"])
        st = _claim_status(pc, built, reduced, bool(flags))
        cmp_ = (pc or {}).get("comparison") or {}
        claims.append({
            "id": t["claim_id"], "metric": t["metric"], "setting": t.get("setting", ""),
            "paper_value": c.value if c else None, "unit": (c.unit if c else "") or t.get("unit_hint", ""),
            "quote": "" if anonymize or not c else c.quote, "location": "" if anonymize or not c else c.location,
            "measured": cmp_.get("measured_mean"), "sd": cmp_.get("measured_sd"), "n": cmp_.get("n"),
            "tolerance": cmp_.get("tolerance"), "delta": cmp_.get("delta"), "status": st,
            "evidence": "" if anonymize or not cmp_ else (pc or {}).get("extraction", {}).get("evidence", "")[:200],
            "method": (pc or {}).get("extraction", {}).get("method"),
        })
    counts = {k: sum(1 for c in claims if c["status"] == k) for k in STATUS}
    n_ok = counts["reproduced"] + counts["reproduced_reduced"]
    n_checked = sum(1 for c in claims if c["status"] not in ("not_measured", "blocked", "untrusted"))

    runs = rob.get("runs", [])
    commands = json.loads((rdir / "commands.json").read_text()) if (rdir / "commands.json").exists() else []
    script = (rdir / "paperci_run.sh").read_text(encoding="utf-8", errors="replace") if (rdir / "paperci_run.sh").exists() else ""
    pip = sorted(set((build or {}).get("pip_packages", [])))
    trusted = built and not flags
    patch = verified_patch((rdir / "repo.diff").read_text(encoding="utf-8", errors="replace")) \
        if trusted and (rdir / "repo.diff").exists() else []
    lock = (rdir / "requirements.lock.txt").read_text(encoding="utf-8", errors="replace") \
        if trusted and (rdir / "requirements.lock.txt").exists() else ""
    if trusted and "reproduce.sh" not in str(snap.get("tree") or ""):
        rf = reproduce_file(script)
        if rf:
            patch.append(rf)
    manifests = snap.get("manifests") or {}
    if isinstance(manifests, str):
        try:
            manifests = json.loads(manifests)
        except Exception:
            manifests = {}
    manifest_text = " ".join(str(v) for v in manifests.values()).lower()

    # ---- CI-style checks
    checks = [
        ("ok", "Sandbox", "Fresh Docker container: non-root, all capabilities dropped, egress only via an allowlist proxy"),
        ("ok" if cl.get("n_claims") else "na", "Claims sealed",
         f"{cl.get('n_claims', 0)} headline claims extracted from the paper text and sealed (sha256 "
         f"{(cl.get('sealed_sha256') or '')[:12]}) before the builder started; the builder never saw the values"),
    ]
    if build:
        checks.append(("ok" if built else "bad", "Build",
                       f"{build.get('status')} after {build.get('turns')} agent turns, {build.get('seconds', 0) / 60:.1f} min"
                       + (f" (scale: {build.get('scale')})" if built else
                          ("" if anonymize else f": {(build.get('reason') or '')[:160]}"))))
    if runs:
        ok_runs = sum(1 for r in runs if r["exit_code"] == 0)
        checks.append(("ok" if ok_runs == len(runs) else ("warn" if ok_runs else "bad"), "Offline reruns",
                       f"{ok_runs}/{len(runs)} runs exited 0 in fresh containers with --network none (seeds "
                       f"{', '.join(str(r['seed']) for r in runs)})"))
    if dec:
        for f, (name, good, bad) in INTEGRITY_TEXT.items():
            failed = f in flags
            extra = f" ({(dec.get('integrity') or {}).get('replay_reason')})" if failed and f == "precomputed_results_replay" else ""
            checks.append(("bad" if failed else "ok", name, (bad + extra) if failed else good))
        denied = (dec.get("integrity") or {}).get("egress_denied") or {}
        if denied:
            checks.append(("warn", "Egress", "Refused hosts: " + ", ".join(f"{h} ({n}x)" for h, n in denied.items())))
    if orc:
        checks.append(("ok" if orc["reward"] >= 0.999 else "bad", "Oracle replay",
                       f"replayed the solution in a new environment: reward {orc['reward']}"))
    if trn:
        checks.append(("ok", "Trainability", f"cheap solver ({MODELS['solver']}): {trn['label']} "
                       f"({sum(a['reward'] >= 0.999 for a in trn['attempts'])}/{len(trn['attempts'])} solved)"))
    if tri.get("decision") == "reject":
        checks.insert(0, ("bad", "Triage", "Not runnable in a CPU sandbox: " + "; ".join(
            tri.get("rule_reasons") or [((tri.get("llm") or {}).get("reason") or "not CPU-feasible")[:200]])))
    if tri.get("decision") == "accept" and not cl.get("n_claims"):
        checks.append(("bad", "Claims", "No headline number in the paper text could be grounded in a verbatim quote"))

    # ---- actionable suggestions for authors
    sugg = []
    ok_reruns = sum(1 for x in runs if x["exit_code"] == 0)
    proof = (f"PaperCI applied it in the sandbox; with it, {ok_reruns}/{len(runs)} offline reruns in fresh containers "
             f"exited 0" + (f" and {sum(1 for c in claims if c['status'] in ('reproduced', 'reproduced_reduced'))} "
                            f"claim(s) reproduced" if claims else ""))
    code_chg = [f for f in patch if f["path"] != "reproduce.sh"]
    if code_chg:
        sugg.append(("Commit the verified patch", f"Your code needed {len(code_chg)} file change(s) to run from a clean "
                     f"checkout ({', '.join('`' + f['path'] + '`' for f in code_chg[:5])}). {proof}. "
                     "Apply it with `git apply paperci.patch` (shown below as suggested changes)."))
    if any(f["path"] == "reproduce.sh" for f in patch):
        sugg.append(("Add the verified one-command entry point", "`reproduce.sh` (below) is the script PaperCI wrote and "
                     f"verified to regenerate your numbers. {proof}. Commit it so readers can rerun the paper in one command."))
    if lock and (not manifest_text or "==" not in manifest_text):
        sugg.append(("Pin the environment that worked", f"PaperCI recorded the exact package versions of the environment "
                     f"that reproduced your run ({lock.count('==')} pins). {proof}. Download it as `requirements.lock.txt`."))
    if pip and not manifest_text:
        sugg.append(("Add a dependency manifest", "The repository has no requirements.txt / pyproject. PaperCI had to "
                     f"discover these by trial and error: `{' '.join(pip)}`. Ship a pinned requirements.txt."))
    elif pip:
        missing = [p for p in pip if p.lower().split("[")[0] not in manifest_text]
        if missing:
            sugg.append(("Declare missing dependencies", f"Installed during the build but not in your manifests: `{' '.join(missing)}`."))
    if build and build.get("status") == "gave_up":
        cat = build.get("give_up_category", "other")
        txt = GIVE_UP_ADVICE.get(cat, "The agent stopped: \"{reason}\"").format(
            reason="(details hidden)" if anonymize else (build.get("reason") or "")[:300])
        sugg.append(("Make the result reproducible from the public repo", txt))
    if build and build.get("status") in ("max_turns", "wall_clock"):
        sugg.append(("Add a one-command reproduction entry point", "Getting this repository to produce the headline "
                     f"numbers took more than {build.get('turns')} agent steps. A `reproduce.sh` (or a README section) "
                     "that prints each table number would make it checkable."))
    if reduced and build.get("scale_notes"):
        notes = "" if anonymize else f": {build['scale_notes'][:300]}"
        sugg.append(("Publish a small config with expected numbers", "The full experiment exceeds a CPU budget, so it "
                     f"was run scaled down{notes}. A documented small config with its expected "
                     "output would let anyone verify the pipeline in minutes."))
    seen_cat = {f["category"] for f in (build or {}).get("failures", [])}
    for cat, txt in FAILURE_ADVICE.items():
        if cat in seen_cat:
            sugg.append((cat.replace("_", " ").capitalize(), txt))
    nm = [c for c in claims if c["status"] == "not_measured"]
    if built and nm:
        sugg.append(("Print every reported number", "No script output contained "
                     + ", ".join(f"{c['id']} ({c['metric']}{', ' + c['setting'] if c['setting'] else ''})" for c in nm[:4])
                     + ". Printing them from the evaluation script makes them verifiable."))
    for c in claims:
        if c["status"] == "differs":
            sugg.append((f"Check claim {c['id']}", f"Measured {_fmt(c['measured'])} +/- {_fmt(c['sd'])} over {c['n']} offline runs "
                         f"vs {_fmt(c['paper_value'])} in the paper. The reported number may come from a different "
                         "config, seed set or checkpoint than the public code's defaults."))

    if built and not flags and n_checked:
        headline = (f"{n_ok} of {len(claims)} claims reproduced" + (" at reduced scale" if reduced and n_ok else "")
                    + f", {n_checked} measured")
        state = "ok" if n_ok else "warn"
    elif built and flags:
        headline, state = "Build ran, but the run failed integrity checks; no claim is trusted", "bad"
    elif built:
        headline, state = "Code runs, but none of the claimed metrics were printed", "warn"
    elif build:
        why = {"data_unavailable": "evaluation data not released", "needs_credentials": "needs API credentials",
               "gpu_required": "needs a GPU", "too_large": "too large for a CPU sandbox"}.get(build.get("give_up_category"))
        if build.get("status") == "gave_up":
            headline = f"Blocked: {why or 'agent stopped early'}"
        elif build.get("status") in ("max_turns", "wall_clock"):
            headline = f"Not runnable within the agent budget ({build.get('turns')} turns)"
        else:
            headline = f"Could not reproduce in a CPU sandbox ({build.get('status')})"
        state = "bad"
    elif tri.get("decision") == "reject":
        headline, state = "Not runnable in a CPU sandbox today (triage)", "bad"
    elif tri.get("decision") == "accept" and not cl.get("n_claims"):
        headline, state = "No verifiable numeric claims found in the paper text", "warn"
    else:
        headline, state = "Not attempted", "na"

    pipeline = []
    for st, lab, shared in (("triage", "Triage", True), ("claims", "Claims sealed", True), ("build", "Build", False),
                            ("verify", "Offline run", False), ("robustness", "Reruns", False), ("verdict", "Verdict", False),
                            ("package", "Package", False), ("oracle", "Oracle", False), ("trainability", "Trainability", False)):
        row = db.get_stage(aid, st, "shared" if shared else label)
        out = json.loads(row["output"]) if row and row["output"] else {}
        if not row or row["status"] != "done":
            state_ = "na"
        elif st == "triage":
            state_ = "ok" if out.get("decision") == "accept" else "bad"
        elif st == "build":
            state_ = "ok" if out.get("status") == "built" else "bad"
        elif st in ("verify", "robustness"):
            rr = out.get("runs", [])
            state_ = "na" if not rr else ("ok" if all(r["exit_code"] == 0 for r in rr) else "bad")
        elif st == "verdict":
            state_ = "bad" if flags else ("ok" if out.get("verdict") in ("verified", "toy-scale") else "warn")
        elif st == "package":
            state_ = "ok" if out.get("task_id") else ("bad" if out.get("invalidated") else "na")
        elif st == "oracle":
            state_ = "ok" if out.get("reward", 0) >= 0.999 else "bad"
        else:
            state_ = "ok"
        secs = (row["finished_at"] - row["started_at"]) if row and row["finished_at"] and row["started_at"] else None
        pipeline.append((lab, state_, secs))

    cost = ledger.paper_total(aid, label) + sum(ledger.paper_total(aid, f"{label}-solver{i}") for i in range(1, 4))
    stage_rows = db.q("SELECT trace_id FROM stages WHERE arxiv_id=? AND run_label=? AND stage='build'", (aid, label))
    who = anon_id(aid) if anonymize else f"arXiv:{aid}"
    return {
        "arxiv_id": None if anonymize else aid, "who": who, "label": label,
        "title": f"Paper {anon_id(aid)}" if anonymize else paper["title"],
        "repo": None if anonymize else snap.get("full_name"), "sha": None if anonymize else snap.get("sha"),
        "headline": headline, "state": state, "verdict": dec.get("verdict"), "confidence": dec.get("confidence"),
        "reason": "" if anonymize else dec.get("reason", ""), "claims": claims, "counts": counts, "checks": checks, "suggestions": sugg,
        "build": build or {}, "runs": runs, "pipeline": pipeline, "commands": [] if anonymize else commands, "script": "" if anonymize else script,
        "pip": pip, "task_id": pkg.get("task_id"), "oracle": orc, "trainability": trn,
        "patch": [] if anonymize else patch, "patch_summary": [(f["path"] if not anonymize else "file", f["added"], f["removed"]) for f in patch],
        "lock": "" if anonymize else lock, "proof": proof,
        "cost_usd": round(cost, 4), "trace_id": stage_rows[0]["trace_id"] if stage_rows else None,
        "sealed_sha": cl.get("sealed_sha256"), "models": dict(MODELS), "generated": time.strftime("%Y-%m-%d %H:%M"),
        "walkthrough": _walkthrough(build, runs, pip, snap, anonymize, reduced),
    }


def _walkthrough(build, runs, pip, snap, anonymize, reduced) -> str:
    if not build:
        return "PaperCI has not attempted this paper yet."
    repo = "the paper's repository" if anonymize else f"`{snap.get('full_name')}` at commit `{(snap.get('sha') or '')[:10]}`"
    s = (f"PaperCI fetched {repo} into a fresh CPU sandbox. A builder agent ({MODELS['builder']}) worked for "
         f"{build.get('turns')} turns ({build.get('seconds', 0) / 60:.1f} min)")
    s += f", installed {len(pip)} package(s) ({', '.join(pip[:8])})" if pip else ", with no extra packages"
    if build.get("status") == "built":
        s += (f", and wrote a single run script that passed an offline dry run. An independent verifier "
              f"({MODELS['verifier']}, a different model family) then ran it {len(runs)} times in new containers with "
              "the network disconnected, read only the output files, and compared each number with the sealed claim "
              "using a tolerance calibrated from the spread across reruns.")
        if reduced:
            s += " The run was declared reduced-scale, so matching numbers are reported as reproduced at reduced scale."
    else:
        s += f", but did not reach a runnable state ({build.get('status')})" + (
            "" if anonymize else f": {(build.get('reason') or '')[:240]}")
    return s


# ---------------- renderers ----------------
def render_md(r: dict) -> str:
    icon = {"ok": "PASS", "warn": "WARN", "bad": "FAIL", "na": "SKIP"}
    L = [f"## PaperCI review: {r['title']}", "",
         f"**{r['headline']}**  ·  verdict `{r['verdict']}` ({r['confidence']})" if r["verdict"] else f"**{r['headline']}**", "",
         "Pipeline: " + " → ".join(f"{lab} `{icon[s]}`" for lab, s, _ in r.get("pipeline", [])), "",
         "### Walkthrough", r["walkthrough"], "",
         "### Claims vs measured", "", "| claim | paper | PaperCI measured | tolerance | status |", "|---|---|---|---|---|"]
    for c in r["claims"]:
        meas = f"{_fmt(c['measured'])} ± {_fmt(c['sd'])} (n={c['n']})" if c["measured"] is not None else "-"
        L.append(f"| **{c['id']}** {c['metric']}{' — ' + c['setting'] if c['setting'] else ''} | {_fmt(c['paper_value'])}{c['unit'] if c['unit'] == '%' else ''} "
                 f"| {meas} | {_fmt(c['tolerance'])} | {STATUS[c['status']][0]} |")
    L += ["", "### Checks"] + [f"- `{icon[s]}` **{n}**: {t}" for s, n, t in r["checks"]]
    if r["suggestions"]:
        L += ["", "### Suggestions for the authors"] + [f"{i}. **{t}.** {b}" for i, (t, b) in enumerate(r["suggestions"], 1)]
    if r.get("patch"):
        L += ["", "### Verified suggested changes", "", f"_{r['proof']}._ Apply all with `git apply paperci.patch`.", ""]
        for f in r["patch"][:8]:
            L += [f"<details><summary><code>{f['path']}</code> (+{f['added']} −{f['removed']}){' new file' if f['new'] else ''}</summary>",
                  "", "```diff", f["diff"][:6000].rstrip(), "```", "", "</details>"]
    if r.get("lock"):
        L += ["", "<details><summary>requirements.lock.txt (the environment that reproduced the run)</summary>", "",
              "```", r["lock"].strip()[:6000], "```", "", "</details>"]
    if r["script"]:
        L += ["", "<details><summary>Reproduce it yourself</summary>", "", "```bash", r["script"].strip()[:4000], "```",
              "", "</details>"]
    L += ["", f"<sub>PaperCI · builder {r['models']['builder']} · verifier {r['models']['verifier']} · cost ${r['cost_usd']} · "
          f"trace `{r['trace_id']}` · sealed sha256 `{(r['sealed_sha'] or '')[:16]}`</sub>"]
    return "\n".join(L) + "\n"


CSS = """
:root{--fg:#1f2328;--muted:#59636e;--line:#d1d9e0;--bg:#f6f8fa;--ok:#1a7f37;--warn:#9a6700;--bad:#d1242f;--na:#59636e;--acc:#8250df}
*{box-sizing:border-box}body{margin:0;font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;color:var(--fg);background:#fff}
.wrap{max-width:1012px;margin:0 auto;padding:24px 16px 64px}
.pr{border-bottom:1px solid var(--line);padding-bottom:16px;margin-bottom:20px}.pr h1{font-size:26px;font-weight:400;margin:0 0 6px}
.pr h1 b{font-weight:600}.pill{display:inline-flex;align-items:center;gap:6px;border-radius:2em;padding:4px 12px;color:#fff;font-weight:600;font-size:13px}
.pill.ok{background:var(--ok)}.pill.warn{background:var(--warn)}.pill.bad{background:var(--bad)}.pill.na{background:var(--na)}
.meta{color:var(--muted);margin-left:8px}
.comment{display:flex;gap:12px;margin:18px 0}.avatar{flex:0 0 40px;height:40px;border-radius:50%;background:linear-gradient(135deg,#8250df,#1f6feb);
color:#fff;font-weight:700;display:flex;align-items:center;justify-content:center;font-size:13px}
.box{flex:1;border:1px solid var(--line);border-radius:6px;overflow:hidden}.boxh{background:var(--bg);border-bottom:1px solid var(--line);padding:8px 16px;color:var(--muted)}
.boxh b{color:var(--fg)}.bot{border:1px solid var(--line);border-radius:2em;padding:0 7px;font-size:12px;margin-left:4px}
.boxb{padding:4px 16px 16px}h3{font-size:16px;margin:20px 0 8px;padding-bottom:6px;border-bottom:1px solid var(--line)}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{border:1px solid var(--line);padding:6px 10px;text-align:left;vertical-align:top}th{background:var(--bg)}
.st{display:inline-block;border-radius:2em;padding:1px 9px;font-size:12px;font-weight:600;white-space:nowrap;border:1px solid}
.st.ok{color:var(--ok);border-color:#4ac26b;background:#dafbe1}.st.warn{color:var(--warn);border-color:#d4a72c;background:#fff8c5}
.st.bad{color:var(--bad);border-color:#ff8182;background:#ffebe9}.st.na{color:var(--na);border-color:var(--line);background:var(--bg)}
.q{color:var(--muted);font-size:12px;margin-top:3px}.mono{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px}
.checks{border:1px solid var(--line);border-radius:6px}.ck{display:flex;gap:10px;padding:8px 14px;border-top:1px solid var(--line)}.ck:first-child{border-top:0}
.ck .i{width:16px;flex:0 0 16px;font-weight:700}.ck .i.ok{color:var(--ok)}.ck .i.bad{color:var(--bad)}.ck .i.warn{color:var(--warn)}.ck .i.na{color:var(--na)}
.ck .n{font-weight:600;min-width:120px}
.sg{border:1px solid var(--line);border-left:3px solid var(--acc);border-radius:6px;padding:10px 14px;margin:8px 0}.sg b{display:block;margin-bottom:2px}
details{border:1px solid var(--line);border-radius:6px;margin-top:14px}summary{cursor:pointer;padding:8px 14px;background:var(--bg);font-weight:600}
pre{margin:0;padding:12px 14px;overflow:auto;background:#0d1117;color:#e6edf3;font-size:12px;line-height:1.45}
.bar{display:flex;height:10px;border-radius:6px;overflow:hidden;margin:10px 0 4px;background:var(--bg)}.bar span{display:block}
.pipe{display:flex;gap:6px;flex-wrap:wrap;margin:6px 0 4px}.stp{display:flex;align-items:center;gap:6px;border:1px solid var(--line);
border-radius:6px;padding:4px 10px;font-size:12px;font-weight:600;background:#fff}.stp .q{margin:0;font-weight:400}
.stp.ok{border-color:#4ac26b}.stp.ok .i{color:var(--ok)}.stp.bad{border-color:#ff8182}.stp.bad .i{color:var(--bad)}
.stp.warn{border-color:#d4a72c}.stp.warn .i{color:var(--warn)}.stp.na{color:var(--muted)}
.foot{color:var(--muted);font-size:12px;margin-top:18px}.sum{display:flex;gap:18px;flex-wrap:wrap;color:var(--muted);font-size:13px}
.sum b{color:var(--fg);font-size:18px;display:block}
.chg{border:1px solid var(--line);border-radius:6px;margin:10px 0;overflow:hidden}.chgh{display:flex;justify-content:space-between;
padding:6px 12px;background:var(--bg);border-bottom:1px solid var(--line);font-size:12px}.pa{color:var(--ok);font-weight:600}.pd{color:var(--bad);font-weight:600}
pre.diff{background:#fff;color:var(--fg);padding:0;max-height:420px}.dl{display:block;padding:0 12px;white-space:pre}.dl.a{background:#dafbe1}
.dl.d{background:#ffebe9}.dl.h{color:var(--muted);background:#f6f8fa}.vfy{color:var(--ok);font-weight:600;margin:4px 0 8px}
.dlrow{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:10px 0}.dlb{border:1px solid var(--line);border-radius:6px;padding:5px 12px;
background:var(--bg);color:var(--fg);text-decoration:none;font-weight:600;font-size:13px}.dlb:hover{background:#eaeef2}
"""

ICON = {"ok": "&#10003;", "bad": "&#10007;", "warn": "!", "na": "&ndash;"}


def render_html(r: dict) -> str:
    e = html.escape
    c = r["counts"]
    tot = max(len(r["claims"]), 1)
    seg = [("ok", c["reproduced"] + c["reproduced_reduced"]), ("warn", c["match_1"] + c["differs_reduced"] + c["differs_1"]),
           ("bad", c["differs"] + c["untrusted"]), ("na", c["not_measured"] + c["blocked"])]
    color = {"ok": "#1a7f37", "warn": "#d4a72c", "bad": "#d1242f", "na": "#d1d9e0"}
    bar = "".join(f'<span style="width:{100 * n / tot:.1f}%;background:{color[k]}"></span>' for k, n in seg if n)
    rows = []
    for x in r["claims"]:
        lab, cls, desc = STATUS[x["status"]]
        meas = (f"<b>{_fmt(x['measured'])}</b> &plusmn; {_fmt(x['sd'])}<div class=q>n={x['n']} offline runs</div>"
                if x["measured"] is not None else "<span class=q>&ndash;</span>")
        quote = (f'<div class=q>&ldquo;{e(x["quote"][:220])}&rdquo;{" &middot; " + e(x["location"]) if x["location"] else ""}</div>'
                 if x["quote"] else "")
        ev = f'<div class="q mono">{e(x["evidence"][:140])}</div>' if x["evidence"] else ""
        rows.append(f"<tr><td><b>{x['id']}</b> {e(x['metric'])}{(' &middot; ' + e(x['setting'])) if x['setting'] else ''}{quote}</td>"
                    f"<td><b>{_fmt(x['paper_value'])}</b>{e(x['unit']) if x['unit'] == '%' else ''}</td><td>{meas}{ev}</td>"
                    f"<td>{_fmt(x['tolerance'])}</td><td><span class='st {cls}' title='{e(desc)}'>{lab}</span></td></tr>")
    checks = "".join(f"<div class=ck><span class='i {s}'>{ICON[s]}</span><span class=n>{e(n)}</span><span>{e(t)}</span></div>"
                     for s, n, t in r["checks"])
    sugg = "".join(f"<div class=sg><b>{i}. {e(t)}</b>{_md_inline(b)}</div>" for i, (t, b) in enumerate(r["suggestions"], 1))
    repo = (f'<a href="https://github.com/{e(r["repo"])}/tree/{e(r["sha"] or "")}">{e(r["repo"])}</a> @ '
            f'<span class=mono>{e((r["sha"] or "")[:10])}</span>') if r["repo"] else "repository hidden"
    if r["arxiv_id"] and r["arxiv_id"].startswith("u-"):
        arx = "uploaded PDF (private)"
    elif r["arxiv_id"]:
        arx = f'<a href="https://arxiv.org/abs/{e(r["arxiv_id"])}">arXiv:{e(r["arxiv_id"])}</a>'
    else:
        arx = e(r["who"])
    b = r["build"]
    steps = "".join(f"<div class='stp {s}'><span class=i>{ICON[s]}</span>{e(lab)}"
                    f"<span class=q>{(f'{t / 60:.1f}m' if t >= 60 else f'{t:.0f}s') if t else ''}</span></div>"
                    for lab, s, t in r.get("pipeline", []))
    sugchg = ""
    if r.get("patch"):
        from urllib.parse import quote as _q
        full = "".join(f["diff"] for f in r["patch"])
        cards = []
        for f in r["patch"][:8]:
            lines = []
            for ln in f["diff"].splitlines()[1:400]:
                cls = "a" if ln.startswith("+") and not ln.startswith("+++") else ("d" if ln.startswith("-") and not ln.startswith("---")
                                                                                   else ("h" if ln.startswith(("@@", "---", "+++")) else ""))
                lines.append(f"<span class='dl {cls}'>{e(ln)}</span>")
            cards.append(f"<div class=chg><div class=chgh><span class=mono>{e(f['path'])}</span>"
                         f"<span><span class=pa>+{f['added']}</span> <span class=pd>&minus;{f['removed']}</span>"
                         f"{' &middot; new file' if f['new'] else ''}</span></div><pre class=diff>{''.join(lines)}</pre></div>")
        sugchg = (f"<h3>Verified suggested changes</h3><div class=vfy>&#10003; {e(r['proof'])}.</div>{''.join(cards)}"
                  f"<div class=dlrow><a class=dlb download='paperci.patch' href='data:text/x-diff;charset=utf-8,{_q(full)}'>"
                  f"Download paperci.patch</a><span class=mono>git apply paperci.patch</span>"
                  + (f"<a class=dlb download='requirements.lock.txt' href='data:text/plain;charset=utf-8,{_q(r['lock'])}'>"
                     f"Download requirements.lock.txt</a>" if r.get("lock") else "") + "</div>")
    elif r.get("lock"):
        from urllib.parse import quote as _q
        sugchg = (f"<h3>Verified environment</h3><div class=vfy>&#10003; {e(r['proof'])}.</div><div class=dlrow>"
                  f"<a class=dlb download='requirements.lock.txt' href='data:text/plain;charset=utf-8,{_q(r['lock'])}'>"
                  f"Download requirements.lock.txt</a><span class=q>{r['lock'].count('==')} pinned packages</span></div>")
    repro = ""
    if r["script"]:
        repro = (f"<details><summary>Reproduce it yourself</summary><pre>{e(r['script'].strip()[:6000])}</pre></details>"
                 + (f"<details><summary>Every command the builder ran ({len(r['commands'])})</summary><pre>"
                    + e("\n".join('$ ' + x for x in r['commands'])[:8000]) + "</pre></details>" if r["commands"] else "")
                 + (f"<details><summary>Harbor task</summary><pre>harbor run -p registry/tasks/{e(r['task_id'])}\n"
                    f"# instruction.md, environment/Dockerfile, solution/solve.sh, tests/test.sh (hidden verifier)</pre></details>"
                    if r["task_id"] else ""))
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PaperCI review: {e(r['title'])}</title><style>{CSS}</style></head><body><div class=wrap>
<div class=pr><h1><b>{e(r['title'])}</b></h1>
<span class="pill {r['state']}">{ICON[r['state']]} {e(r['headline'])}</span> {badge_svg(r)}<span class=meta>{arx} &middot; {repo} &middot; run <span class=mono>{e(r['label'])}</span></span></div>
<div class=comment><div class=avatar>PCI</div><div class=box>
<div class=boxh><b>paperci</b><span class=bot>bot</span> reviewed this paper's code &middot; {e(r['generated'])}</div><div class=boxb>
<h3>Pipeline</h3><div class=pipe>{steps}</div>
<h3>Walkthrough</h3><p>{_md_inline(r['walkthrough'])}</p>
<div class=sum><div><b>{len(r['claims'])}</b>claims sealed</div><div><b>{c['reproduced'] + c['reproduced_reduced']}</b>reproduced</div>
<div><b>{sum(1 for x in r['claims'] if x['measured'] is not None)}</b>measured</div><div><b>{len(r['runs'])}</b>offline reruns</div>
<div><b>{b.get('turns', '-')}</b>agent turns</div><div><b>${r['cost_usd']:.2f}</b>total cost</div></div>
<div class=bar>{bar}</div>
<h3>Claims vs measured</h3><table><tr><th>Claim (from the paper)</th><th>Paper</th><th>PaperCI measured</th><th>Tolerance</th><th>Status</th></tr>{''.join(rows)}</table>
<h3>Checks</h3><div class=checks>{checks}</div>
{('<h3>Suggestions for the authors</h3>' + sugg) if sugg else ''}
{sugchg}
{repro}
<div class=foot>Builder {e(r['models']['builder'])} &middot; verifier {e(r['models']['verifier'])} (different model family) &middot;
neatlogs trace <span class=mono>{e(str(r['trace_id']))}</span> &middot; sealed claims sha256 <span class=mono>{e((r['sealed_sha'] or '')[:16])}</span><br>
Verdict <b>{e(str(r['verdict']))}</b> ({e(str(r['confidence']))}): {e(r['reason'][:300])}</div>
</div></div></div></div></body></html>"""


def _md_inline(s: str) -> str:
    s = html.escape(s)
    return re.sub(r"`([^`]+)`", r"<code class=mono>\1</code>", s)


def render_terminal(r: dict) -> str:
    from .live import C
    col = {"ok": C["green"], "warn": C["yellow"], "bad": C["red"], "na": C["dim"]}
    g = {"ok": "+", "warn": "!", "bad": "x", "na": "-"}
    R = C["reset"]
    L = ["", f"{C['bold']}PaperCI review{R}  {r['who']}  {r['title'][:90]}",
         f"  {col[r['state']]}{g[r['state']]} {r['headline']}{R}" + (f"   verdict {r['verdict']} ({r['confidence']})" if r["verdict"] else ""),
         "", f"{C['bold']}  Claims vs measured{R}"]
    for c in r["claims"]:
        lab, cls, _ = STATUS[c["status"]]
        name = f"{c['id']} {c['metric']}" + (f" ({c['setting']})" if c["setting"] else "")
        meas = f"{_fmt(c['measured'])} +/- {_fmt(c['sd'])} n={c['n']}" if c["measured"] is not None else "-"
        L.append(f"  {col[cls]}{g[cls]}{R} {name[:58]:<58} paper {_fmt(c['paper_value']):>8}   measured {meas:<22} {col[cls]}{lab}{R}")
    ok = sum(1 for s, _, _ in r["checks"] if s == "ok")
    bad = [(n, t) for s, n, t in r["checks"] if s == "bad"]
    L += ["", f"{C['bold']}  Checks{R}  {C['green']}{ok} passed{R}" + (f", {C['red']}{len(bad)} failed{R}" if bad else "")]
    L += [f"  {C['red']}x{R} {n}: {t}" for n, t in bad]
    if r["suggestions"]:
        L += ["", f"{C['bold']}  Suggestions for the authors{R}"]
        L += [f"  {i}. {t}" for i, (t, _) in enumerate(r["suggestions"], 1)]
    L += ["", f"  {C['dim']}cost ${r['cost_usd']:.3f} · builder {r['models']['builder']} · verifier {r['models']['verifier']}"
          f" · neatlogs trace {r['trace_id']}{R}"]
    return "\n".join(L)


def badge_svg(r: dict) -> str:
    """shields.io-style badge an author can put in their README."""
    c = r["counts"]
    n_ok = c["reproduced"] + c["reproduced_reduced"]
    if c["untrusted"]:
        msg, color = "integrity check failed", "#e05d44"
    elif r["state"] == "bad" or not r["build"]:
        msg, color = ("not reproduced" if r["build"] else "pending"), ("#e05d44" if r["build"] else "#9f9f9f")
    else:
        msg = f"{n_ok}/{len(r['claims'])} claims reproduced" + (" (reduced)" if c["reproduced_reduced"] else "")
        color = "#4c1" if n_ok and not c["reproduced_reduced"] else ("#97ca00" if n_ok else "#dfb317")
    lw, rw = 56, 7 * len(msg) + 12
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{lw + rw}" height="20" role="img" aria-label="PaperCI: {msg}">'
            f'<linearGradient id="s" x2="0" y2="100%"><stop offset="0" stop-color="#bbb" stop-opacity=".1"/><stop offset="1" stop-opacity=".1"/></linearGradient>'
            f'<clipPath id="r"><rect width="{lw + rw}" height="20" rx="3" fill="#fff"/></clipPath><g clip-path="url(#r)">'
            f'<rect width="{lw}" height="20" fill="#555"/><rect x="{lw}" width="{rw}" height="20" fill="{color}"/>'
            f'<rect width="{lw + rw}" height="20" fill="url(#s)"/></g><g fill="#fff" text-anchor="middle" '
            f'font-family="Verdana,Geneva,DejaVu Sans,sans-serif" font-size="11">'
            f'<text x="{lw / 2}" y="14">PaperCI</text><text x="{lw + rw / 2}" y="14">{html.escape(msg)}</text></g></svg>')


def write(aid: str, label: str, anonymize: bool = False) -> Path:
    r = build_review(aid, label, anonymize)
    REVIEWS.mkdir(parents=True, exist_ok=True)
    stem = (anon_id(aid) if anonymize else aid.replace("/", "_")) + f"-{label}"
    (REVIEWS / f"{stem}.md").write_text(render_md(r), encoding="utf-8", newline="\n")
    (REVIEWS / f"{stem}.svg").write_text(badge_svg(r), encoding="utf-8", newline="\n")
    p = REVIEWS / f"{stem}.html"
    p.write_text(render_html(r), encoding="utf-8", newline="\n")
    return p


def write_index(anonymize: bool = False) -> Path:
    """A pull-request-list style page of every reviewed run."""
    rows = db.q("SELECT s.arxiv_id, s.run_label FROM stages s WHERE s.stage='verdict' AND s.status='done' "
                "ORDER BY s.finished_at DESC")
    items = []
    for row in rows:
        try:
            p = write(row["arxiv_id"], row["run_label"], anonymize)
            r = build_review(row["arxiv_id"], row["run_label"], anonymize)
        except Exception:
            continue
        items.append((p.name, r))
    e = html.escape
    li = "".join(
        f"<a class=row href='{e(n)}'><span class='i {r['state']}'>{ICON[r['state']]}</span><span class=t><b>{e(r['title'][:110])}</b>"
        f"<span class=q>{e(r['who'])} &middot; run {e(r['label'])} &middot; {e(r['headline'])} &middot; ${r['cost_usd']:.2f}</span></span></a>"
        for n, r in items)
    css = CSS + ".row{display:flex;gap:12px;padding:12px 16px;border-top:1px solid var(--line);color:inherit;text-decoration:none}" \
                ".row:hover{background:var(--bg)}.row .i{font-weight:700;width:16px}.row .i.ok{color:var(--ok)}.row .i.bad{color:var(--bad)}" \
                ".row .i.warn{color:var(--warn)}.row .i.na{color:var(--na)}.t{display:flex;flex-direction:column}.list{border:1px solid var(--line);border-radius:6px}"
    n_claims = sum(len(r["claims"]) for _, r in items)
    n_rep = sum(r["counts"]["reproduced"] + r["counts"]["reproduced_reduced"] for _, r in items)
    n_meas = sum(sum(1 for c in r["claims"] if c["measured"] is not None) for _, r in items)
    n_flag = sum(1 for _, r in items if r["counts"]["untrusted"])
    page = (f"<!doctype html><meta charset=utf-8><title>PaperCI reviews</title><style>{css}</style><div class=wrap>"
            f"<div class=pr><h1><b>PaperCI</b> &middot; reviews</h1><span class=meta>CI for arXiv: every fresh paper with code gets "
            f"its claims re-run in a fresh sandbox and checked by an independent verifier.</span>"
            f"<div class=sum style='margin-top:12px'><div><b>{len(items)}</b>runs reviewed</div><div><b>{n_claims}</b>claims sealed</div>"
            f"<div><b>{n_meas}</b>measured offline</div><div><b>{n_rep}</b>reproduced</div><div><b>{n_flag}</b>runs caught by integrity checks</div></div></div>"
            f"<div class=list>{li}</div></div>")
    out = REVIEWS / ("index-public.html" if anonymize else "index.html")
    out.write_text(page, encoding="utf-8", newline="\n")
    return out
