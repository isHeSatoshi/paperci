"""Static registry dashboard (registry/index.html or docs/dashboard_public.html).

--public anonymizes papers (P-001...) because we do not publish verdicts about
named papers; the private view links arXiv IDs and neatlogs trace IDs."""
from __future__ import annotations

import hashlib
import html
import json
import time

from . import db
from .config import REGISTRY_DIR, ROOT
from .recovery import load_playbook
from .stats import funnel

VERDICT_COLORS = {"verified": "#1a7f37", "falsified-candidate": "#cf222e", "toy-scale": "#9a6700",
                  "inconclusive": "#6e7781", "blocked-infra": "#8250df"}


def _anon(aid: str) -> str:
    return "P-" + hashlib.sha256(aid.encode()).hexdigest()[:6]


def _bar(label: str, n: int, total: int, color: str = "#0969da") -> str:
    w = 0 if not total else max(2, int(380 * n / total))
    return (f"<div class=row><span class=lbl>{html.escape(label)}</span><span class=bar style='width:{w}px;"
            f"background:{color}'></span><span class=num>{n}</span></div>")


def render(public: bool = False) -> str:
    f = funnel()
    runs = f["runs"]
    total = f["watched_with_code"] or 1
    parts = [f"<h1>PaperCI registry</h1><p class=sub>CI for arXiv: fresh code papers, sealed claims, "
             f"independent offline verification. Generated {time.strftime('%Y-%m-%d %H:%M')}. "
             f"{'Public view: papers anonymized.' if public else 'Private view.'}</p>"]
    parts.append("<h2>Funnel (all measured)</h2>")
    parts.append(_bar("watched (has code)", f["watched_with_code"], total))
    parts.append(_bar("passed triage", f["triage_accept"], total))
    parts.append(_bar("grounded sealed claims", f["claims_extracted"], total))
    att = sum(r["papers_attempted"] for r in runs.values())
    parts.append(_bar("build attempts (all runs)", att, total))
    parts.append(_bar("built (offline dry run passed)", sum(r["built"] for r in runs.values()), total))
    parts.append(_bar("metric measured offline", sum(r["metric_measured"] for r in runs.values()), total))
    parts.append(_bar("packaged tasks", sum(r["packaged"] for r in runs.values()), total))
    parts.append(_bar("oracle-validated tasks", sum(r["oracle_validated"] for r in runs.values()), total))
    parts.append("<h3>Triage reject reasons</h3>" + "".join(_bar(k, v, f["triaged"] or 1, "#8c959f") for k, v in f["reject_reasons"].items()))

    parts.append("<h2>Runs (before / after self-improvement)</h2><table><tr><th>run</th><th>attempted</th><th>built</th>"
                 "<th>build rate</th><th>measured</th><th>verdicts</th><th>avg turns</th><th>LLM $</th><th>$/attempt</th>"
                 "<th>$/validated task</th><th>failures (recovered/total)</th></tr>")
    for label, r in sorted(runs.items()):
        vd = ", ".join(f"{k}:{v}" for k, v in r["verdicts"].items())
        parts.append(f"<tr><td>{label}</td><td>{r['papers_attempted']}</td><td>{r['built']}</td><td>{r['build_success_rate']}</td>"
                     f"<td>{r['metric_measured']}</td><td>{html.escape(vd)}</td><td>{r['avg_builder_turns']}</td>"
                     f"<td>{r['llm_cost_usd']}</td><td>{r['cost_per_attempt']}</td><td>{r['cost_per_validated_task']}</td>"
                     f"<td>{r['failures_recovered']}/{r['failures_total']}</td></tr>")
    parts.append("</table>")

    pb = load_playbook()
    parts.append(f"<h2>Learned recovery playbook (generation {pb.generation})</h2><ul>"
                 + "".join(f"<li><b>{html.escape(r.rule_id)}</b> [{html.escape(r.category)}]: {html.escape(r.advice)}</li>" for r in pb.rules)
                 + "</ul>" if pb.rules else "<h2>Learned recovery playbook</h2><p>generation 0 (empty)</p>")

    cats: dict[str, int] = {}
    for r in runs.values():
        for k, v in r["failures"].items():
            cats[k] = cats.get(k, 0) + v
    parts.append("<h2>Failure taxonomy (builder, all runs)</h2>" + "".join(_bar(k, v, max(cats.values() or [1]), "#bf8700") for k, v in sorted(cats.items(), key=lambda kv: -kv[1])))

    parts.append("<h2>Papers</h2><table><tr><th>paper</th><th>split</th><th>run</th><th>verdict</th><th>confidence</th>"
                 "<th>build</th><th>turns</th><th>$</th>" + ("" if public else "<th>neatlogs trace</th>") + "<th>task</th></tr>")
    rows = db.q("SELECT v.arxiv_id, v.run_label, v.output vo, v.trace_id, b.output bo, p.split, p.title FROM stages v "
                "JOIN stages b ON b.arxiv_id=v.arxiv_id AND b.run_label=v.run_label AND b.stage='build' "
                "JOIN papers p ON p.arxiv_id=v.arxiv_id WHERE v.stage='verdict' AND v.status='done' ORDER BY v.run_label, v.arxiv_id")
    for r in rows:
        vo, bo = json.loads(r["vo"]), json.loads(r["bo"])
        pk = db.q1("SELECT output FROM stages WHERE arxiv_id=? AND run_label=? AND stage='package' AND status='done'", (r["arxiv_id"], r["run_label"]))
        task = json.loads(pk["output"]).get("task_id") if pk else None
        name = _anon(r["arxiv_id"]) if public else f"<a href='https://arxiv.org/abs/{r['arxiv_id']}'>{r['arxiv_id']}</a> {html.escape(r['title'][:50])}"
        col = VERDICT_COLORS.get(vo["verdict"], "#000")
        parts.append(f"<tr><td>{name}</td><td>{r['split']}</td><td>{r['run_label']}</td><td style='color:{col};font-weight:600'>{vo['verdict']}</td>"
                     f"<td>{vo['confidence']}</td><td>{bo.get('status')}</td><td>{bo.get('turns')}</td><td>{bo.get('cost_usd')}</td>"
                     + ("" if public else f"<td><code>{r['trace_id']}</code></td>") + f"<td>{'yes' if task else '-'}</td></tr>")
    parts.append("</table>")
    parts.append(f"<p class=sub>Total LLM spend recorded: ${f['total_llm_spend_usd']} (front half ${f['front_half_cost_usd']}).</p>")
    css = ("body{font:14px/1.45 -apple-system,Segoe UI,Arial;margin:32px;max-width:1150px;color:#1f2328}"
           "h1{margin-bottom:0}.sub{color:#57606a}.row{display:flex;align-items:center;margin:3px 0}"
           ".lbl{width:260px}.bar{height:14px;border-radius:3px;display:inline-block}.num{margin-left:8px;font-weight:600}"
           "table{border-collapse:collapse;margin:8px 0;font-size:13px}td,th{border:1px solid #d0d7de;padding:4px 8px;text-align:left}"
           "th{background:#f6f8fa}code{font-size:11px}")
    doc = f"<!doctype html><meta charset=utf-8><title>PaperCI registry</title><style>{css}</style>" + "\n".join(parts)
    out = (ROOT / "docs" / "dashboard_public.html") if public else (REGISTRY_DIR / "index.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(doc, encoding="utf-8")
    return str(out)
