"""Public live board: what the always-on agent is doing, learning and repairing, right now.

Anonymized by construction: papers appear only as P-xxxxxx ids, and only anonymized review pages are linked."""
from __future__ import annotations

import html
import json
import time

from . import db, ledger, ops
from .recovery import load_playbook

ICON = {"cycle": "&#8635;", "review": "&#9998;", "learn": "&#9733;", "chaos": "&#9889;", "incident": "&#10010;",
        "digest": "&#9776;", "budget": "$", "error": "!", "boot": "&#9650;"}
COLOR = {"cycle": "#6366f1", "review": "#0ea5e9", "learn": "#a855f7", "chaos": "#f59e0b", "incident": "#22c55e",
         "digest": "#64748b", "budget": "#64748b", "error": "#ef4444", "boot": "#22c55e"}
_cache: dict = {"ts": 0, "html": ""}


def _ago(ts: float) -> str:
    d = time.time() - ts
    if d < 90:
        return f"{int(d)}s ago"
    if d < 5400:
        return f"{int(d / 60)}m ago"
    if d < 172800:
        return f"{d / 3600:.1f}h ago"
    return f"{int(d / 86400)}d ago"


def _days(n: int = 7) -> list[dict]:
    out = []
    midnight = ((time.time() + ops.TZ_OFF) // 86400) * 86400 - ops.TZ_OFF
    for i in range(n - 1, -1, -1):
        start = midnight - i * 86400
        t = time.gmtime(start + ops.TZ_OFF)
        rows = db.q("SELECT output FROM stages WHERE stage='build' AND status='done' AND finished_at>=? AND finished_at<?",
                    (start, start + 86400))
        built = sum(1 for r in rows if json.loads(r["output"]).get("status") == "built")
        cost = db.q1("SELECT COALESCE(SUM(cost_usd),0) c FROM spend WHERE ts>=? AND ts<?", (start, start + 86400))["c"]
        out.append({"day": time.strftime("%a %d", t), "attempts": len(rows), "built": built, "cost": cost})
    return out


def _generations() -> list[dict]:
    rows = db.q("SELECT output FROM stages WHERE stage='build' AND status='done'")
    g: dict[int, list[int]] = {}
    for r in rows:
        o = json.loads(r["output"])
        if o.get("variant") != "v2":
            continue
        k = int(o.get("playbook_generation") or 0)
        g.setdefault(k, [0, 0])
        g[k][0] += 1
        g[k][1] += o.get("status") == "built"
    return [{"gen": k, "attempts": v[0], "built": v[1]} for k, v in sorted(g.items())]


def _bars(days: list[dict]) -> str:
    mx = max([d["attempts"] for d in days] + [1])
    cols = []
    for d in days:
        h1, h2 = 110 * d["attempts"] / mx, 110 * d["built"] / mx
        cols.append(f"<div class=bc><div class=bs><div class=ba style='height:{h1:.0f}px'></div><div class=bb style='height:{h2:.0f}px'></div></div>"
                    f"<div class=bl>{d['day']}</div><div class=bn>{d['built']}/{d['attempts']} &middot; ${d['cost']:.2f}</div></div>")
    return "<div class=chart>" + "".join(cols) + "</div>"


def render() -> str:
    if time.time() - _cache["ts"] < 20:
        return _cache["html"]
    from .stats import funnel
    e = html.escape
    f = funnel()
    runs = f["runs"].values()
    attempts = sum(r["papers_attempted"] for r in runs)
    built = sum(r["built"] for r in runs)
    within = sum(r["claims_within_tol_n2"] for r in runs)
    caught = sum(r["integrity_flagged"] for r in runs)
    validated = sum(r["oracle_validated"] for r in runs)
    ev = ops.events(400)
    pb = load_playbook()
    cap = ledger.daily_cap()
    today = ledger.today_total()
    last = ev[-1]["ts"] if ev else None
    wd = ops._load(ops.WATCH_STATE)
    up = "live" if last and time.time() - last < 3 * ops.CYCLE_EVERY else "idle"
    healed = [x for x in ev if x["kind"] == "incident"]
    drills = [x for x in ev if x["kind"] == "chaos"]
    learn = [x for x in ev if x["kind"] == "learn"]
    reviews = [x for x in ev if x["kind"] == "review"][-12:]
    feed = "".join(
        f"<div class=ev><span class=ei style='background:{COLOR.get(x['kind'], '#64748b')}'>{ICON.get(x['kind'], '&middot;')}</span>"
        f"<div><div>{e(x['text'])}</div><div class=muted>{_ago(x['ts'])} &middot; {e(x['kind'])}</div></div></div>"
        for x in reversed(ev[-40:])) or "<div class=muted>The agent has not logged any activity yet.</div>"
    gens = _generations()
    gtab = "".join(f"<tr><td>gen {g['gen']}</td><td>{g['attempts']}</td><td>{g['built']}</td>"
                   f"<td><div class=gbar><span style='width:{100 * g['built'] / max(g['attempts'], 1):.0f}%'></span></div>"
                   f"{100 * g['built'] / max(g['attempts'], 1):.0f}%</td></tr>" for g in gens)
    rules = "".join(f"<li><b>{e(r.rule_id)}</b> <span class=muted>[{e(r.category)}]</span> {e(r.advice[:160])}</li>" for r in pb.rules[-8:])
    revs = "".join(f"<a class=rv href='/board/r/{e(x.get('page', ''))}'><span class='dotx {e(x.get('state', 'na'))}'></span>"
                   f"<b>{e(x.get('paper', ''))}</b> {e(x['text'].split(': ', 1)[-1])}<span class=muted> &middot; {_ago(x['ts'])}</span></a>"
                   for x in reversed(reviews)) or "<div class=muted>Reviews from the autopilot will appear here.</div>"
    drill_rows = "".join(f"<div class=dr><span class='dotx {'ok' if x.get('recovered') else 'bad'}'></span>{e(x['text'])}"
                         f"<span class=muted> &middot; {_ago(x['ts'])}</span></div>" for x in reversed(drills[-5:])) or \
        "<div class=muted>First drill runs tonight.</div>"
    inc_rows = "".join(f"<div class=dr><span class='dotx {'ok' if x.get('healed') else 'bad'}'></span>{e(x['text'])}"
                       f"<span class=muted> &middot; {_ago(x['ts'])}</span></div>" for x in reversed(healed[-6:])) or \
        "<div class=muted>No incidents so far. The watchdog checks every 3 minutes.</div>"
    learn_rows = "".join(f"<div class=dr><span class='dotx ok'></span>{e(x['text'])}<span class=muted> &middot; {_ago(x['ts'])}</span></div>"
                         for x in reversed(learn[-4:])) or "<div class=muted>The first nightly learning run happens at 03:00 IST.</div>"
    body = f"""
<div class=wrap style="padding-top:8px">
<div class=bhead><div><span class='live {up}'><i></i>{'Agent running' if up == 'live' else 'Agent idle'}</span>
<h1 style="font-size:36px;margin:10px 0 4px">PaperCI live board</h1>
<div class=muted>An autonomous agent that watches arXiv, rebuilds each new paper's code in a locked-down sandbox, verifies every
headline claim offline with a different model family, learns from its own failures every night, and repairs itself.
Papers are anonymized here.</div></div>
<div class=muted style="text-align:right">last activity {(_ago(last) if last else 'never')}<br>watchdog {(_ago(wd['last_run']) if wd.get('last_run') else 'not run yet')}
{f"&middot; disk {wd.get('disk_free_gb')} GB free" if wd.get('disk_free_gb') else ''}<br>LLM today ${today:.2f}{f' of ${cap:.2f} cap' if cap else ''}</div></div>
<div class=kpis>
<div class=kpi><b>{f['watched_with_code']}</b>papers with code watched</div><div class=kpi><b>{f['triage_accept']}</b>runnable on CPU</div>
<div class=kpi><b>{f['claims_total']}</b>claims sealed</div><div class=kpi><b>{attempts}</b>reproduction attempts</div>
<div class=kpi><b>{built}</b>built &amp; run offline</div><div class=kpi><b>{within}</b>claims reproduced (n&ge;2)</div>
<div class=kpi><b>{caught}</b>shortcuts caught</div><div class=kpi><b>{validated}</b>replayable environments</div></div>
<div class=bgrid>
<div><div class=box><h4>Last 7 days &middot; built / attempted &middot; LLM $</h4>{_bars(_days())}</div>
<div class=box><h4>Latest reviews (anonymized)</h4>{revs}</div>
<div class=box><h4>It gets better every night</h4>{learn_rows}
<table class=gt><tr><th>playbook</th><th>attempts</th><th>built</th><th>build rate</th></tr>{gtab}</table>
<div class=muted style="margin:10px 0 4px">Current playbook generation {pb.generation}: {len(pb.rules)} learned rules. A rule is adopted only if
its regex matches its own failure class and almost nothing else, and its advice passes an integrity filter (no hardcoding,
no skipping verification, allowlisted hosts only). Held-out papers never feed the learner.</div><ul class=rules>{rules}</ul></div></div>
<div><div class=box><h4>Activity</h4><div class=feed>{feed}</div></div>
<div class=box><h4>Self-healing &middot; incidents repaired automatically</h4>{inc_rows}</div>
<div class=box><h4>Chaos drills &middot; breaking itself on purpose, nightly</h4>{drill_rows}</div></div></div>
<div class=foot>PaperCI &middot; generated {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())} &middot; traces by neatlogs &middot; sessions by Entire</div></div>
<script>setTimeout(()=>location.reload(),30000)</script>"""
    _cache.update(ts=time.time(), html=body)
    return body


CSS = """
.bhead{display:flex;justify-content:space-between;gap:24px;align-items:flex-end;padding:12px 0 18px}
.live{display:inline-flex;align-items:center;gap:8px;font-weight:700;font-size:13px;border-radius:99px;padding:4px 12px;background:#dcfce7;color:#166534}
.live i{width:9px;height:9px;border-radius:50%;background:#22c55e;box-shadow:0 0 0 0 #22c55e88;animation:pulse 1.6s infinite}
.live.idle{background:#f1f5f9;color:#475569}.live.idle i{background:#94a3b8;animation:none}
@keyframes pulse{70%{box-shadow:0 0 0 8px #22c55e00}100%{box-shadow:0 0 0 0 #22c55e00}}
.kpis{display:grid;grid-template-columns:repeat(8,1fr);gap:10px;margin-bottom:18px}.kpi{border:1px solid #e2e8f0;border-radius:12px;padding:10px 12px;font-size:12px;color:#64748b}
.kpi b{display:block;font-size:24px;color:#0f172a;letter-spacing:-.02em}
.bgrid{display:grid;grid-template-columns:1.1fr .9fr;gap:18px;align-items:start}
.chart{display:flex;gap:10px;align-items:flex-end;height:170px}.bc{flex:1;text-align:center}.bs{position:relative;height:115px}
.ba,.bb{position:absolute;bottom:0;left:18%;right:18%;border-radius:6px 6px 0 0}.ba{background:#e0e7ff}.bb{background:#6366f1;left:30%;right:30%}
.bl{font-size:12px;font-weight:600;margin-top:4px}.bn{font-size:11px;color:#64748b}
.feed{max-height:640px;overflow:auto}.ev{display:flex;gap:10px;padding:8px 0;border-bottom:1px solid #f1f5f9;font-size:14px}
.ei{flex:0 0 26px;height:26px;border-radius:8px;color:#fff;display:flex;align-items:center;justify-content:center;font-size:13px}
.rv{display:block;padding:7px 0;border-bottom:1px solid #f1f5f9;color:inherit;text-decoration:none;font-size:14px}.rv:hover{background:#f8fafc}
.dotx{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:8px;background:#94a3b8}.dotx.ok{background:#22c55e}.dotx.bad{background:#ef4444}.dotx.warn{background:#f59e0b}
.dr{padding:6px 0;border-bottom:1px solid #f1f5f9;font-size:14px}.gt{width:100%;border-collapse:collapse;margin-top:10px;font-size:13px}
.gt td,.gt th{text-align:left;padding:5px 6px;border-bottom:1px solid #f1f5f9}.gbar{display:inline-block;width:90px;height:8px;background:#f1f5f9;border-radius:9px;margin-right:6px;vertical-align:middle}
.gbar span{display:block;height:8px;background:#a855f7;border-radius:9px}.rules{margin:6px 0 0;padding-left:18px;font-size:13px}.rules li{margin:4px 0}
@media(max-width:980px){.kpis{grid-template-columns:repeat(4,1fr)}.bgrid{grid-template-columns:1fr}}
"""
