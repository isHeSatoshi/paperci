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


def _queue(split: str | None, limit: int, ids: list[str] | None, label: str) -> list[str]:
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
    sql += (" AND p.arxiv_id NOT IN (SELECT arxiv_id FROM stages WHERE stage='verdict' AND run_label=? AND status='done')"
            " ORDER BY p.published DESC LIMIT ?")
    args += [label, limit]
    return [r["arxiv_id"] for r in db.q(sql, tuple(args))]


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
            from .pipeline import reproduce
            for pid in _queue(a.split, a.limit, a.ids, a.label):
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
            for pid in _queue(a.split, a.reproduce, None, lab):
                try:
                    print("reproduce", pid, json.dumps(reproduce(pid, lab, BUILDER_VARIANT)), flush=True)
                except ledger.BudgetExceeded as ex:
                    print("BUDGET STOP:", ex); break
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
