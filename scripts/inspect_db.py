"""Developer inspection helper (local only; prints sealed values, never run in CI or demos)."""
import json
import sqlite3
import sys
from pathlib import Path

c = sqlite3.connect(Path(__file__).resolve().parent.parent / "data" / "paperci.sqlite")
what = sys.argv[1] if len(sys.argv) > 1 else "accepted"
if what == "accepted":
    q = "select s.arxiv_id, s.output from stages s where stage='triage' and json_extract(output,'$.decision')='accept'"
    for aid, out in c.execute(q):
        o = json.loads(out)
        f = o["facts"]
        t, split = c.execute("select title, split from papers where arxiv_id=?", (aid,)).fetchone()
        print(aid, split, f["size_mb"], "MB py", f["py_files"], "|", t[:60], "|", o["llm"]["est_runtime_minutes"], "min")
elif what == "claims":
    row = c.execute("select payload from sealed_claims where arxiv_id=?", (sys.argv[2],)).fetchone()
    for cl in json.loads(row[0]):
        print(cl["claim_id"], cl["metric"], cl["value"], cl["unit"], "|", cl["setting"][:70], "|", cl["location"])
elif what == "stage":
    row = c.execute("select status, output, error from stages where arxiv_id=? and stage=? and run_label=?",
                    (sys.argv[2], sys.argv[3], sys.argv[4])).fetchone()
    print(row[0]); print((row[1] or "")[:6000]); print((row[2] or "")[-3000:])
elif what == "verdicts":
    like = sys.argv[2] if len(sys.argv) > 2 else "%"
    for aid, lab, out in c.execute("select arxiv_id, run_label, output from stages where stage='verdict' and run_label like ? "
                                   "order by arxiv_id, run_label", (like,)):
        o = json.loads(out)
        print(aid, lab, o["verdict"], o["confidence"], "|", o["reason"][:120])
elif what == "compare":
    for lab, out in c.execute("select run_label, output from stages where stage='verdict' and arxiv_id=?", (sys.argv[2],)):
        o = json.loads(out)
        print(lab, o["verdict"], o["confidence"], "|", o["reason"][:200])
        for cid, pc in o["per_claim"].items():
            print("   ", cid, pc["metric"][:40], "raw", pc.get("raw_values"), "|", json.dumps(pc.get("comparison"))[:220])
elif what == "tasks":
    for aid, lab, st, out in c.execute("select arxiv_id, run_label, stage, output from stages where stage in "
                                       "('package','oracle','trainability') and status='done' order by arxiv_id, run_label, stage"):
        print(aid, lab, st, (out or "")[:160])
elif what == "failures":
    for r in c.execute("select arxiv_id, run_label, stage, category, recovered, substr(detail,1,160) from failures order by id desc limit 40"):
        print(r)
