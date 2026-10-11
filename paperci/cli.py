"""PaperCI command line.

  python -m paperci watch                       # poll arXiv + HF daily papers
  python -m paperci prepare --limit 30          # snapshot + triage + sealed claims
  python -m paperci run --split dev --label dev-v1 --variant v1 --limit 3
  python -m paperci improve --labels dev-v1     # self-improvement: mine traces -> playbook
  python -m paperci stats | dashboard | budget
"""
from __future__ import annotations

import argparse
import json
import sys

from . import db, ledger, tracing
from .config import BUILDER_VARIANT


def _queue(split: str | None, limit: int, ids: list[str] | None, label: str, fresh: bool = False) -> list[str]:
    if ids:
        return ids
    sql = ("SELECT p.arxiv_id FROM papers p JOIN stages s ON s.arxiv_id=p.arxiv_id AND s.stage='claims' "
           "AND s.run_label='shared' AND s.status='done' "
           "JOIN stages t ON t.arxiv_id=p.arxiv_id AND t.stage='triage' AND t.run_label='shared' AND t.status='done' "
           "WHERE json_extract(s.output,'$.n_claims')>0 AND json_extract(t.output,'$.decision')='accept'")
    args: list = []
    if split:
        sql += " AND p.split=?"
        args.append(split)
    sql += " AND p.arxiv_id NOT IN (SELECT arxiv_id FROM stages WHERE stage='verdict' AND run_label=? AND status='done')"
    if fresh:  # autopilot: never re-spend on a paper some earlier run already attempted
        sql += " AND p.arxiv_id NOT IN (SELECT arxiv_id FROM stages WHERE stage='build' AND status IN ('done','budget'))"
    sql += " ORDER BY p.published DESC LIMIT ?"
    args += [label, limit]
    return [r["arxiv_id"] for r in db.q(sql, tuple(args))]


def _review(a) -> int:
    import os
    import time as _t
    from . import live, review, sources
    from .stages.watcher import split_for
    if a.pdf:
        from pathlib import Path as _P
        pdf_bytes = _P(a.pdf).read_bytes()
        aid = sources.upload_id(pdf_bytes)
        if not db.q1("SELECT 1 FROM papers WHERE arxiv_id=?", (aid,)):
            p = sources.ingest_file(pdf_bytes, a.repo, a.title, filename=_P(a.pdf).name)
            if not p["repo_url"]:
                print("no code repository: pass --repo https://github.com/owner/name")
                return 2
            p["split"] = "upload"
            db.upsert_paper(p)
    elif a.paper:
        aid = sources.parse_arxiv_id(a.paper)
    else:
        print("give an arXiv id/URL or --pdf")
        return 2
    if a.from_run:
        print(review.render_terminal(review.build_review(aid, a.from_run, a.anonymize)))
        p = review.write(aid, a.from_run, a.anonymize)
        print(f"\n  review: {p}")
        if a.open:
            os.startfile(p)  # noqa: S606 - local file the user asked to open
        return 0
    live.enable()
    live.header(f"PaperCI  ·  reviewing {'uploaded paper ' + aid if aid.startswith('u-') else 'arXiv:' + aid}")
    tracing.init(a.label, a.variant)
    try:
        if not db.q1("SELECT 1 FROM papers WHERE arxiv_id=?", (aid,)):
            live.emit("start", "Fetch paper metadata and find its code")
            p = sources.fetch_paper(aid)
            if a.repo:
                p["repo_url"] = sources.find_github(a.repo)
            if not p["repo_url"]:
                live.emit("fail", "no public code repository found (pass --repo)")
                return 2
            p["split"] = split_for(aid)
            db.upsert_paper(p)
            live.emit("ok", f"{p['title'][:90]}")
        if a.repo and sources.find_github(a.repo):
            db.set_paper(aid, repo_url=sources.find_github(a.repo))
        row = db.q1("SELECT title, repo_url FROM papers WHERE arxiv_id=?", (aid,))
        if not row["repo_url"]:
            live.emit("fail", "no public code repository found (pass --repo)")
            return 2
        live.emit("info", f"{row['title'][:100]}  ·  {row['repo_url']}")
        from .pipeline import prepare, reproduce
        t0 = _t.time()
        prep = prepare(aid)
        print(f"PREPARED: {json.dumps({'paper_id': aid, **prep})}", flush=True)
        if a.prepare_only:
            return 0
        if prep.get("triage") != "accept" or not prep.get("n_claims"):
            live.emit("warn", "stopping: " + ("triage rejected the paper for a CPU sandbox" if prep.get("triage") != "accept"
                                              else "no verifiable numeric claims in the paper text"))
            p = review.write(aid, a.label, a.anonymize)
            print(f"\nREVIEW_PATH: {p}", flush=True)
            return 0
        live.header("Reproduce")
        hints = open(a.hints_file, encoding="utf-8").read() if a.hints_file else ""
        focus = [x.strip() for x in a.focus.split(",") if x.strip()] if a.focus else None
        if hints or focus:
            live.emit("info", f"using the author's guidance: focus {focus or 'all claims'}"
                              + (f", {len(hints)} chars of notes (sealed values redacted)" if hints else ""))
        out = reproduce(aid, a.label, a.variant, hints, focus)
        live.emit("ok", f"pipeline finished in {(_t.time() - t0) / 60:.1f} min, ${out.get('cost_usd', 0):.3f}")
        r = review.build_review(aid, a.label, a.anonymize)
        print(review.render_terminal(r))
        p = review.write(aid, a.label, a.anonymize)
        review.write_index()
        print(f"\nREVIEW_PATH: {p}", flush=True)
        if a.open:
            os.startfile(p)  # noqa: S606
    except ledger.BudgetExceeded as ex:
        print("BUDGET STOP:", ex)
        return 3
    finally:
        tracing.shutdown()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="paperci")
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("watch"); w.add_argument("--per-query", type=int, default=60); w.add_argument("--since")
    p = sub.add_parser("prepare"); p.add_argument("--limit", type=int, default=20); p.add_argument("--ids", nargs="*")
    p.add_argument("--split")
    r = sub.add_parser("run"); r.add_argument("--split"); r.add_argument("--label", required=True)
    r.add_argument("--variant", default=BUILDER_VARIANT); r.add_argument("--limit", type=int, default=1)
    r.add_argument("--ids", nargs="*")
    i = sub.add_parser("improve"); i.add_argument("--labels", nargs="+", required=True); i.add_argument("--apply", action="store_true")
    e = sub.add_parser("envlearn"); e.add_argument("--min-papers", type=int, default=2); e.add_argument("--apply", action="store_true")
    cy = sub.add_parser("cycle", help="one bot tick: watch -> prepare new -> reproduce next N -> dashboard")
    cy.add_argument("--prepare", type=int, default=20); cy.add_argument("--reproduce", type=int, default=1)
    cy.add_argument("--split", default=None); cy.add_argument("--label", default=None)
    cy.add_argument("--fresh", action="store_true", help="only papers no earlier run has attempted")
    rv = sub.add_parser("review", help="CodeRabbit-style review of one paper: run the pipeline live, write the review")
    rv.add_argument("paper", nargs="?", help="arXiv id or URL"); rv.add_argument("--repo", help="override the code repository URL")
    rv.add_argument("--pdf", "--file", dest="pdf", help="review an unpublished paper from a local file: PDF, LaTeX "
                    "(.tex or .zip/.tar.gz source), DOCX, HTML, Markdown or text (needs --repo unless the paper links it)")
    rv.add_argument("--title"); rv.add_argument("--prepare-only", action="store_true")
    rv.add_argument("--hints-file", help="author notes passed to the builder (redacted against sealed values)")
    rv.add_argument("--focus", help="comma-separated claim ids the author wants verified first")
    rv.add_argument("--label", default="review"); rv.add_argument("--variant", default=BUILDER_VARIANT)
    rv.add_argument("--from-run", help="only render the review of an existing run label (no new spend)")
    rv.add_argument("--anonymize", action="store_true"); rv.add_argument("--open", action="store_true")
    sub.add_parser("reviews", help="re-render every review + the PR-list style index")
    sub.add_parser("stats"); sub.add_parser("budget")
    d = sub.add_parser("dashboard"); d.add_argument("--public", action="store_true")
    a = ap.parse_args(argv)

    if a.cmd == "budget":
        print(json.dumps(ledger.summary(), indent=1, default=str)); return 0
    if a.cmd == "stats":
        from .stats import funnel
        print(json.dumps(funnel(), indent=1, default=str)); return 0
    if a.cmd == "dashboard":
        from .dashboard import render
        print(render(public=a.public)); return 0
    if a.cmd == "reviews":
        from . import review
        print(review.write_index(), review.write_index(anonymize=True)); return 0
    if a.cmd == "review":
        return _review(a)

    label = getattr(a, "label", None) or a.cmd
    variant = getattr(a, "variant", "-")
    tracing.init(label, variant)
    try:
        if a.cmd == "watch":
            from .stages.watcher import watch
            print(json.dumps(watch(per_query=a.per_query, since=a.since)))
        elif a.cmd == "prepare":
            from .pipeline import prepare
            ids = a.ids or [r["arxiv_id"] for r in db.q(
                "SELECT arxiv_id FROM papers WHERE status='new'" + (" AND split=?" if a.split else "")
                + " ORDER BY published DESC LIMIT ?", ((a.split, a.limit) if a.split else (a.limit,)))]
            for pid in ids:
                try:
                    print(pid, json.dumps(prepare(pid)), flush=True)
                except ledger.BudgetExceeded as ex:
                    print("BUDGET STOP:", ex); break
                except Exception as ex:
                    print(pid, "prepare failed:", type(ex).__name__, str(ex)[:200], flush=True)
        elif a.cmd == "run":
            import shutil
            from pathlib import Path
            from .pipeline import reproduce
            for pid in _queue(a.split, a.limit, a.ids, a.label):
                free_gb = shutil.disk_usage(Path.home().anchor).free / 1e9
                if free_gb < 5:
                    print(f"DISK STOP: only {free_gb:.1f} GB free on the Docker host disk", flush=True)
                    break
                try:
                    print(pid, json.dumps(reproduce(pid, a.label, a.variant)), flush=True)
                except ledger.BudgetExceeded as ex:
                    print("BUDGET STOP:", ex); break
                except Exception as ex:
                    print(pid, "run failed:", type(ex).__name__, str(ex)[:300], flush=True)
        elif a.cmd == "cycle":
            import time as _t
            from .dashboard import render
            from .pipeline import prepare, reproduce
            from .stages.watcher import watch
            lab = a.label or "live-" + _t.strftime("%Y%m%d")
            print("watch:", json.dumps(watch()), flush=True)
            for r in db.q("SELECT arxiv_id FROM papers WHERE status='new' ORDER BY published DESC LIMIT ?", (a.prepare,)):
                try:
                    print("prepare", r["arxiv_id"], json.dumps(prepare(r["arxiv_id"])), flush=True)
                except ledger.BudgetExceeded as ex:
                    print("BUDGET STOP:", ex); break
                except Exception as ex:
                    print("prepare failed", r["arxiv_id"], type(ex).__name__, flush=True)
            import shutil
            from pathlib import Path
            for pid in _queue(a.split, a.reproduce, None, lab, fresh=a.fresh):
                free_gb = shutil.disk_usage(Path.home().anchor).free / 1e9
                if free_gb < 5:
                    print(f"DISK STOP: only {free_gb:.1f} GB free on the Docker host disk", flush=True)
                    break
                try:
                    print("reproduce", pid, json.dumps(reproduce(pid, lab, BUILDER_VARIANT), default=str), flush=True)
                except ledger.BudgetExceeded as ex:
                    print("BUDGET STOP:", ex); break
                except Exception as ex:
                    print("reproduce failed", pid, type(ex).__name__, str(ex)[:300], flush=True)
            print("dashboard:", render(public=False), render(public=True))
        elif a.cmd == "improve":
            from .improver import improve
            print(json.dumps(improve(a.labels, apply=a.apply), indent=1, default=str))
        elif a.cmd == "envlearn":
            from .envlearn import propose
            print(json.dumps(propose(a.min_papers, apply=a.apply), indent=1))
    finally:
        tracing.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
