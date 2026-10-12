"""Offline unit checks (no LLM, no Docker): python tests/test_offline.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from paperci.pipeline import _apply_spec  # noqa: E402
from paperci.recovery import classify  # noqa: E402
from paperci.schemas import Claim  # noqa: E402
from paperci.stages.claims import grounded, targets, value_in_text  # noqa: E402
from paperci.stages.tasks import extraction_spec  # noqa: E402
from paperci.stages.verifier import candidates, compare, extract, integrity, normalize, verdict  # noqa: E402

LOG = """epoch 1 | train loss 0.91 | test accuracy 0.7012
epoch 2 | train loss 0.55 | test accuracy 0.8123
epoch 3 | train loss 0.41 | test accuracy 0.8650
final: coverage=0.9612 avg_set_size=1.62
"""
arts = {"run.log": LOG, "metrics.json": '{"test": {"rmse": 0.231, "n": 100}}'}


def test_extraction():
    c = Claim(claim_id="c1", metric="test accuracy", value=86.9, unit="%", higher_is_better=True)
    e = extract("x", "t", c, arts)  # deterministic path: one template repeated -> last value
    assert e["method"] == "deterministic" and abs(e["value"] - 0.865) < 1e-9, e
    v, how = normalize(c, e["value"])
    assert abs(v - 86.5) < 1e-9 and how.startswith("x100")
    cmp_ = compare(c, [v])
    assert cmp_["within"], cmp_  # |86.5-86.9| = 0.4 <= 1 point floor for percents
    spec = extraction_spec("c1", e, e["value"])
    assert spec and _apply_spec(spec, arts) == 0.865, spec
    cj = Claim(claim_id="c2", metric="RMSE", value=0.23)
    ej = extract("x", "t", cj, arts)
    assert ej["found"] and ej["value"] == 0.231, ej
    sj = extraction_spec("c2", ej, ej["value"])
    assert sj["type"] == "json" and _apply_spec(sj, arts) == 0.231, sj
    cov = candidates("coverage", arts)
    assert cov and cov[-1]["value"] == 0.9612
    # evidence quoted as one flattened line must still match the two-line log it came from
    ml = {"run.log": "--- c2: success rate agent B ---\n  robot_1: stalled=22.7% exitos=76/100\n"}
    flat = {"file": "run.log", "evidence": "--- c2: success rate agent B --- robot_1: stalled=22.7% exitos=76/100"}
    sm = extraction_spec("c2", flat, 76.0)
    assert sm and _apply_spec(sm, ml) == 76.0, sm
    assert _apply_spec(sm, {"run.log": ml["run.log"].replace("76/100", "73/100")}) == 73.0
    hdr = candidates("success rate", {"run.log": "--- c2: success rate agent B (slot 1), 6000 steps ---\n  robot_1: ok=76/100\n"}, "c2")
    assert hdr and hdr[0]["header"] and hdr[0]["after"] == ["robot_1: ok=76/100"], hdr
    assert not candidates("success rate", {"run.log": "success rate over 6000 steps"})


def test_verdicts():
    c = Claim(claim_id="c1", metric="accuracy", value=0.95, higher_is_better=True)
    run = [{"exit_code": 0, "copied_from_repo": []}] * 3
    clean = {"flags": [], "egress_denied": {}}
    far = {"c1": {"extraction": {"method": "deterministic"}, "comparison": compare(c, [0.80, 0.81, 0.80])}}
    near = {"c1": {"extraction": {"method": "deterministic"}, "comparison": compare(c, [0.949, 0.951, 0.95])}}
    assert verdict({"status": "built", "scale": "full"}, run, near, clean)[0] == "verified"
    assert verdict({"status": "built", "scale": "full"}, run, far, clean)[0] == "falsified-candidate"
    assert verdict({"status": "built", "scale": "reduced"}, run, near, clean)[0] == "toy-scale"
    assert verdict({"status": "gave_up"}, [], {}, clean)[0] == "blocked-infra"
    assert verdict({"status": "built", "scale": "full"}, run, near, {"flags": ["hardcoded_claim_value"]})[0] == "inconclusive"


def test_integrity(tmp=Path(__file__).parent / "_tmp_build"):
    tmp.mkdir(exist_ok=True)
    (tmp / "paperci_run.sh").write_text("python train.py --epochs 1\necho 'accuracy: 91.37'\n")
    c = Claim(claim_id="c1", metric="accuracy", value=91.37, unit="%")
    r = integrity(tmp, [c], [{"copied_from_repo": ["./results/final.json"]}], {"denied": {"evil.com": 2}})
    assert "hardcoded_claim_value" in r["flags"] and "prebuilt_result_files" in r["flags"], r
    assert r["hardcoded"][0]["literal"] == "[sealed value]"  # never echo the sealed number
    # A builder-written driver that imports the repo's package is the repo's code path; one that does not, is not.
    import json as _json
    tree = [{"path": "mech_design/__init__.py"}, {"path": "mech_design/mechanism.py"}]
    (tmp / "paperci_run.sh").write_text("cd /work/repo\npython /work/settingA.py\n")
    for body, want in (("import mech_design.mechanism as m\nm.Mechanism().fit()\n", True), ("print(0.5)\n", False)):
        (tmp / "writes.json").write_text(_json.dumps([{"path": "/work/settingA.py", "content": body}]))
        r = integrity(tmp, [], [], {}, tree=tree)
        assert r["repo_code_path_used"] is want, (body, r)
    for f in tmp.iterdir():
        f.unlink()
    tmp.rmdir()


def test_claims_and_classifier():
    text = "Our method reaches a test accuracy of 91.37% on CIFAR-10, beating the baseline (88.1%)."
    good = Claim(claim_id="c1", metric="test accuracy", value=91.37, unit="%", quote="reaches a test accuracy of 91.37% on CIFAR-10")
    bad = Claim(claim_id="c2", metric="test accuracy", value=95.0, unit="%", quote="we reach 95.0% accuracy on CIFAR-10 test set")
    assert grounded(good, text) and not grounded(bad, text)
    t = targets([Claim(claim_id="c1", metric="acc", value=10.5, setting="10.5k samples")])[0]
    assert not value_in_text(10.5, t.metric + " " + t.setting), t  # setting that would leak the value is dropped
    assert classify("Traceback (most recent call last):\nModuleNotFoundError: No module named 'x'", 0, "python a.py | tail") == "missing_module"
    assert classify("raise FileNotFoundError(path)", 0, "cat /work/repo/a.py") is None
    assert classify("curl: (56) CONNECT tunnel failed, response 403\nTunnel connection failed: 403 Filtered", 1) == "egress_denied"
    assert classify("curl: (5) Could not resolve proxy: paperci-egress", 5) == "proxy_unreachable"
    assert classify("ProxyError('Cannot connect to proxy.', OSError('Tunnel connection failed: 403 Filtered'))", 1) == "egress_denied"


def test_paper_formats():
    import io
    import zipfile
    from paperci.sources import paper_text
    main = r"\documentclass{article}\title{Fast \textbf{Things}}\begin{document}\input{sec/results}\end{document}"
    sec = r"\section{Results} Our method reaches 91.37\% test accuracy (Table~\ref{t:1}). % hidden comment"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("main.tex", main)
        z.writestr("sec/results.tex", sec)
    text, title, fmt = paper_text(buf.getvalue(), "source.zip")
    assert fmt == "latex-zip" and "91.37%" in text and "hidden comment" not in text and title == "Fast Things", (fmt, title, text)
    d = io.BytesIO()
    with zipfile.ZipFile(d, "w") as z:
        z.writestr("word/document.xml", "<w:document><w:p><w:r><w:t>Accuracy is 88.1%</w:t></w:r></w:p></w:document>")
    assert "Accuracy is 88.1%" in paper_text(d.getvalue(), "paper.docx")[0]
    assert paper_text(b"<html><title>T</title><body><p>RMSE 0.23</p></body></html>", "p.html")[1] == "T"


def test_verified_patch():
    from paperci.review import reproduce_file, verified_patch
    diff = (
        "diff -ruN --exclude=__pycache__ /opt/orig/repo/train.py /work/repo/train.py\n"
        "--- /opt/orig/repo/train.py\t2026-10-01 10:00:00\n+++ /work/repo/train.py\t2026-10-11 10:00:00\n"
        "@@ -1,2 +1,2 @@\n-import torch.cuda as dev\n+import torch\n x = 1\n"
        "diff -ruN --exclude=__pycache__ /opt/orig/repo/results/metrics.json /work/repo/results/metrics.json\n"
        "--- /opt/orig/repo/results/metrics.json\t1970-01-01 00:00:00\n+++ /work/repo/results/metrics.json\t2026-10-11\n"
        "@@ -0,0 +1 @@\n+{\"acc\": 0.9}\n"
        "diff -ruN --exclude=__pycache__ /opt/orig/repo/pkg.egg-info/PKG-INFO /work/repo/pkg.egg-info/PKG-INFO\n"
        "--- /opt/orig/repo/pkg.egg-info/PKG-INFO\t1970-01-01\n+++ /work/repo/pkg.egg-info/PKG-INFO\t2026-10-11\n@@ -0,0 +1 @@\n+x\n"
        "Binary files /opt/orig/repo/m.pt and /work/repo/m.pt differ\n")
    files = verified_patch(diff)
    assert [f["path"] for f in files] == ["train.py"], files  # generated outputs, egg-info and binaries dropped
    assert files[0]["diff"].startswith("diff --git a/train.py b/train.py\n--- a/train.py\n+++ b/train.py")
    rf = reproduce_file("set -e\ncd /work/repo\npython eval.py > /work/out/log.txt\n")
    assert rf["path"] == "reproduce.sh" and "cd ." in rf["diff"] and "./paperci_out/log.txt" in rf["diff"], rf


def test_daily_cap(monkeypatch=None):
    import os
    import time
    from paperci import db, ledger
    os.environ["PAPERCI_DAILY_CAP"] = "0.0001"
    os.environ["PAPERCI_CAP_SINCE"] = str(time.time() + 3600)  # nothing spent since "deploy" -> one cheap call passes
    try:
        ledger.precheck("-", "test", 0.00005)
        try:
            ledger.precheck("-", "test", 0.01)
            raise AssertionError("daily cap not enforced")
        except ledger.BudgetExceeded as e:
            assert "daily cap" in str(e)
        ledger.precheck("-", "test", 0.01, allow_reserve=True)  # demo/reserve calls are exempt
        os.environ["PAPERCI_AUTHOR_JOB"] = "1"
        ledger.precheck("-", "test", 0.01)  # author submissions are not paced by the autopilot's daily cap
    finally:
        os.environ.pop("PAPERCI_AUTHOR_JOB", None)
        os.environ.pop("PAPERCI_DAILY_CAP")
        os.environ.pop("PAPERCI_CAP_SINCE")
    assert db is not None


def test_worker_health():
    import json
    import time
    from paperci import ops
    saved = ops.HEARTBEAT.read_text() if ops.HEARTBEAT.exists() else None
    try:
        ops.beat()
        assert ops.health()[0] and not ops.busy()
        ops.beat("run")
        assert ops.health()[0] and ops.busy()
        d = json.loads(ops.HEARTBEAT.read_text())
        d["deadline"] = time.time() - ops.GRACE - 1  # busy past its deadline = hung
        ops.HEARTBEAT.write_text(json.dumps(d))
        ok, info = ops.health()
        assert not ok and info["overdue"] and not ops.busy()
        ops.HEARTBEAT.write_text(json.dumps({"ts": time.time() - ops.IDLE_BEAT_MAX - 1, "busy": None}))
        assert not ops.health()[0]  # idle but silent = worker thread dead
    finally:
        if saved is None:
            ops.HEARTBEAT.unlink(missing_ok=True)
        else:
            ops.HEARTBEAT.write_text(saved)


def test_policy_sandbox():
    from paperci import policy_sandbox as ps
    base = ps.BASELINE_PATH.read_text(encoding="utf-8")
    assert ps.contract_test(base) == [], ps.contract_test(base)
    pol = ps.baseline()
    st = {"turn": 8, "max_turns": 18, "run_by_turn": 8, "script_by_turn": 12, "single_target": True, "controller": True,
          "submit_attempts": 0, "fired": [], "failures": [],
          "actions": [{"type": "shell", "cmd": "ls /work/repo", "exit": 0}]}
    assert pol.call("milestone", st)["key"] == "run"  # same behaviour as the old hard-coded controller
    assert pol.call("milestone", {**st, "controller": False}) is None
    assert pol.call("late_nudge", {**st, "turn": 16}).startswith("Only 2 turns left")
    tail = "\ndef system_addendum(state):\n    return ''\n\ndef late_nudge(state):\n    return None\n"
    attacks = {
        "import": "import os\ndef milestone(state):\n    return None" + tail,
        "open": "def milestone(state):\n    open('/etc/passwd')\n    return None" + tail,
        "dunder": "def milestone(state):\n    return state.__class__" + tail,
        "dunder text": "def milestone(state):\n    return {'key': 'x', 'message': '__class__'}" + tail,
        "while": "def milestone(state):\n    while True:\n        pass" + tail,
        "cheat text": "def milestone(state):\n    return {'key': 'x', 'message': 'just hardcode the numbers'}" + tail,
        "unknown key": "def milestone(state):\n    return state['sealed_values']" + tail,
        "pow": "def milestone(state):\n    return 9 ** 9" + tail,
        "missing fn": "def milestone(state):\n    return None\n",
        "bad return": "def milestone(state):\n    return 42" + tail,
    }
    for name, src in attacks.items():
        assert ps.contract_test(src), f"policy attack not caught: {name}"
    # The frozen copy keeps a policy from mutating harness state.
    evil = "def milestone(state):\n    state['actions'].append({'type': 'write'})\n    return None" + tail
    p = ps.Policy(evil, "test")
    p.call("milestone", st)
    assert len(st["actions"]) == 1


def test_evolve_decisions():
    import json
    import tempfile
    from pathlib import Path
    from paperci import evolve, ops
    tmp = Path(tempfile.mkdtemp())
    saved = (evolve.ARCHIVE, evolve.ROUND, evolve.CANDS, ops.EVENTS)
    evolve.ARCHIVE, evolve.ROUND, evolve.CANDS, ops.EVENTS = tmp / "a.json", tmp / "r.json", tmp / "c", tmp / "e.jsonl"
    try:
        a = evolve.archive()
        assert evolve.pick_strategy(a)["id"] == "s-base"  # untried strategies are explored first
        for s, (u, g) in zip(a["strategies"], [(3, 3.0), (3, -3.0), (3, 0.0)]):
            s["uses"], s["gain"] = u, g
        assert evolve.pick_strategy(a)["id"] == "s-base"
        res = lambda f, built=True, flags=(): {"fitness": f, "built": built, "flags": list(flags)}  # noqa: E731
        a["baselines"]["K"] = {"p1": res(2.0), "p2": res(1.0, built=False)}
        for cid, kind in (("r-pol", "policy"), ("r-pb", "playbook"), ("r-x", "policy")):
            a["candidates"][cid] = {"id": cid, "kind": kind, "status": "proposed", "strategy": "s-base"}
        evolve._save(evolve.ARCHIVE, a)
        q = [{"who": "r-pol", "paper": "p1", "status": "done", "result": res(3.0)},
             {"who": "r-pol", "paper": "p2", "status": "done", "result": res(2.5)},   # +2.5: wins
             {"who": "r-pb", "paper": "p1", "status": "done", "result": res(5.0, built=False)},
             {"who": "r-pb", "paper": "p2", "status": "done", "result": res(4.0)},   # better total but regresses p1
             {"who": "r-x", "paper": "p1", "status": "done", "result": res(9.0, flags=["printed_sealed_value"])},
             {"who": "r-x", "paper": "p2", "status": "done", "result": res(9.0)}]
        evolve._save(evolve.ROUND, {"id": "r", "status": "deciding", "bench": ["p1", "p2"], "budget": 1.5,
                                    "incumbent": {"key": "K"}, "strategy": "s-base",
                                    "candidates": ["r-pol", "r-pb", "r-x"], "queue": q})
        evolve.decide()
        c = json.loads(evolve.ARCHIVE.read_text())["candidates"]
        assert c["r-pol"]["status"] == "pending_approval"   # code changes never go live without a human
        assert c["r-pb"]["status"] == "rejected" and "regressed" in c["r-pb"]["reason"]
        assert c["r-x"]["status"] == "rejected" and "integrity" in c["r-x"]["reason"]  # tripwire beats a high score
    finally:
        evolve.ARCHIVE, evolve.ROUND, evolve.CANDS, ops.EVENTS = saved


def test_author_preempts_background_job():
    import sys
    import tempfile
    import time
    from pathlib import Path
    from paperci import ops
    saved = (ops.author_waiting, ops._remove_sandboxes, ops.EVENTS, ops.OPS)
    tmp = Path(tempfile.mkdtemp())
    ops.EVENTS, ops.OPS = tmp / "e.jsonl", tmp
    ops._remove_sandboxes = lambda: 0
    try:
        ops.author_waiting = lambda: False
        rc, out = ops._sub([sys.executable, "-c", "print('hi')"], "t", 60, preemptible=True)
        assert rc == 0 and "hi" in out
        ops.author_waiting = lambda: True
        t0 = time.time()
        rc, _ = ops._sub([sys.executable, "-c", "import time; time.sleep(60)"], "t", 120, preemptible=True)
        assert rc == ops.PREEMPTED and time.time() - t0 < 20
        rc, _ = ops._sub([sys.executable, "-c", "import time; time.sleep(1)"], "t", 60)  # not preemptible
        assert rc == 0
    finally:
        ops.author_waiting, ops._remove_sandboxes, ops.EVENTS, ops.OPS = saved


def test_icml_consensus_and_score():
    import tempfile
    from pathlib import Path
    from paperci import icml_eval as ie
    tmp = Path(tempfile.mkdtemp())
    saved = (ie.ICML, ie.CANDS, ie.STATE, ie.BENCH_FILE, ie._get, ie.ours)
    ie.ICML, ie.CANDS, ie.STATE, ie.BENCH_FILE = tmp, tmp / "c.json", tmp / "s.json", tmp / "b.json"
    lb = lambda o, v, q="high", s="u/x": {"orid": o, "quality": q, "space_id": s,  # noqa: E731
                                         "claims": [{"claim": "C1", "verdict": v, "evidence": "e"}]}
    data = {"papers.json": {"papers": [{"orid": o, "arxiv": a, "title": o, "area": "T"} for o, a in
                                       (("A", "2601.00001"), ("B", "2601.00002"), ("C", "2601.00003"), ("D", "2601.00004"))]},
            "verdicts_final.json": {str(i): x for i, x in enumerate([
                lb("A", "verified"), lb("A", "verified"),                       # clean verified
                lb("B", "falsified"), lb("B", "falsified"), lb("B", "verified", "low"),  # low quality ignored
                lb("C", "verified"), lb("C", "falsified"),                      # contested: excluded
                lb("D", "verified")])}}                                         # one logbook: excluded
    ie._get = lambda name, repo: data[name]
    try:
        out = ie.fetch()
        assert out == {"candidates": 2, "verified": 1, "falsified": 1}, out
        cands = ie._load(ie.CANDS, [])
        for c in cands:
            c["screen"], c["role"] = {"status": "runnable"}, "eval"
        ie._save(ie.CANDS, cands)
        ie._save(ie.STATE, {"runs": [{"arxiv": "2601.00001", "status": "done", "result": {"verdict": "verified", "build": "built"}},
                                     {"arxiv": "2601.00002", "status": "done", "result": {"verdict": "blocked-infra"}}]})
        s = ie.score()
        assert s["runs"] == 2 and s["decided"] == 1 and s["agree"] == 1 and s["built"] == 1, s
        assert s["verified_recovered"] == "1/1" and s["falsified_recovered"] == "0/1", s
        from paperci import web
        page = web.icml_page()
        body = page.body.decode()
        assert "2601.00001" in body and "Automated result" in body, body[:500]
    finally:
        ie.ICML, ie.CANDS, ie.STATE, ie.BENCH_FILE, ie._get, ie.ours = saved


def test_logbook_rules_must_be_general():
    from paperci.logbook_learn import leak_problems
    from paperci.schemas import PlaybookRule
    src = [{"orid": "X", "arxiv": "2601.01234", "space": "alice/mtlr-eigenvalue-repro",
            "title": "Multi-task Linear Regression without Eigenvalue Lower Bounds"}]
    rule = lambda adv: PlaybookRule(rule_id="lb_x", category="missing_module", pattern="No module", advice=adv)  # noqa: E731
    assert not leak_problems(rule("If imports fail, pip install -e . from the repo root before running scripts."), src)
    assert leak_problems(rule("For multi-task linear regression papers, copy the setup of 2601.01234."), src)
    assert leak_problems(rule("Clone alice/mtlr-eigenvalue-repro and follow it."), src)
    assert leak_problems(rule("Expect an accuracy of 0.9312 at the end."), src)


def test_background_queues_alternate():
    import time
    from paperci import evolve, icml_eval, ops
    saved = (ops.enabled, ops._load, evolve.due_new_round, evolve.has_pending, icml_eval.golden_pending,
             icml_eval.has_pending, ops.NIGHTLY_HOUR)
    st = {"nightly_day": time.strftime("%Y-%m-%d", ops._local())}
    ops.enabled, ops._load = (lambda: True), (lambda p: st)
    evolve.due_new_round, evolve.has_pending = (lambda: False), (lambda: True)
    icml_eval.golden_pending, icml_eval.has_pending = (lambda: False), (lambda: True)
    import os
    prio = os.environ.pop("PAPERCI_ICML_PRIORITY", None)
    try:
        assert ops.due() == "evolve"
        st["last_bg"] = "evolve"
        assert ops.due() == "icml"
        st["last_bg"] = "icml"
        assert ops.due() == "evolve"
        os.environ["PAPERCI_ICML_PRIORITY"] = "1"
        assert ops.due() == "icml"
    finally:
        os.environ.pop("PAPERCI_ICML_PRIORITY", None)
        if prio is not None:
            os.environ["PAPERCI_ICML_PRIORITY"] = prio
        (ops.enabled, ops._load, evolve.due_new_round, evolve.has_pending, icml_eval.golden_pending,
         icml_eval.has_pending, ops.NIGHTLY_HOUR) = saved


def test_auto_submit_at_turn_limit():
    from types import SimpleNamespace as NS
    from paperci.stages.builder import Builder
    for script, dry_ok, want in (("yes", True, "built"), ("yes", False, "max_turns"), ("", True, None)):
        b = Builder.__new__(Builder)
        b.sb, b.result, b.turn = NS(exec=lambda *a, **k: NS(output=script)), None, 18

        def tool(name, args, b=b, ok=dry_ok):
            b.result = {"status": "built", "scale": args["scale"]} if ok else None
            return ("accepted" if ok else "REJECTED: exit 1"), ok
        b._tool = tool
        did = b._auto_submit()
        assert did == (want is not None) and (b.result or {}).get("status") == want, (script, dry_ok, b.result)
        if want == "built":
            assert b.result["scale"] == "reduced" and b.result["auto_submitted"]


def test_go_replies_start_the_run():
    from paperci.web import GO
    for t in ("Go ahead with your plan.", "go", "Go!", "do everything", "ok", "start", "yes"):
        assert GO.match(t), t
    for t in ("go with claim c3 first", "no I can't, you understand from the paper", "wait", "use the v2 config"):
        assert not GO.match(t), t


def test_failed_runs_are_marked_for_neatlogs():
    from opentelemetry.trace import StatusCode
    from paperci import tracing
    from paperci.pipeline import run_problem
    clean = {"verdict": "inconclusive", "integrity": {"flags": []}}
    assert run_problem({"status": "max_turns"}, {**clean, "verdict": "blocked-infra"}) == "builder max_turns"
    assert run_problem({"status": "gave_up"}, {**clean, "verdict": "blocked-infra"}) is None  # honest answer
    assert run_problem({"status": "built"}, clean) is None
    assert run_problem({"status": "built"}, {**clean, "verdict": "blocked-infra"}) == "offline verification run failed"
    assert run_problem({"status": "built"}, {**clean, "integrity": {"flags": ["no_repo_code_path"]}}).startswith("integrity")

    class Span:
        def __init__(self):
            self.attrs, self.status = {}, None

        def set_attribute(self, k, v):
            self.attrs[k] = v

        def set_status(self, s):
            self.status = s
    s = Span()
    tracing.io(s, inp={"arxiv_id": "x"}, out="done")
    tracing.fail(s, "builder max_turns")
    assert s.attrs["input.value"] == '{"arxiv_id": "x"}' and s.attrs["output.value"] == "done"
    assert s.status.status_code == StatusCode.ERROR and s.attrs["paperci.failure"] == "builder max_turns"


def test_neatlogs_incident_snapshots():
    import time as _t
    from paperci import db, nl_api
    rows = [{"id": i, "ts": _t.time() - 60 * i, "arxiv_id": f"p{i % 3}", "run_label": "r", "stage": "builder.budget",
             "category": "turn_budget_exhausted" if i < 4 else "egress_denied", "detail": "d", "trace_id": f"t{i}"}
            for i in range(6)]
    orig = db.q
    db.q = lambda sql, args=(): rows
    try:
        snaps = {s["category"]: s for s in nl_api.snapshots()}
        # Occurrences from before a deployed fix for that exact pattern are resolved, not open.
        fixed = {s["category"] for s in nl_api.snapshots(fixes=[{"category": "egress_denied", "like": "d",
                                                                 "live": _t.time() + 10}])}
    finally:
        db.q = orig
    assert fixed == {"turn_budget_exhausted"}
    tb = snaps["turn_budget_exhausted"]
    assert tb["n"] == 4 and tb["papers"] == ["p0", "p1", "p2"] and len(tb["occurrences"]) == 4
    assert {o["trace_id"] for o in tb["occurrences"]} == {"t0", "t1", "t2", "t3"}
    assert len({o["source_event_id"] for o in tb["occurrences"]}) == 4 and "playbook" in tb["description"]
    assert snaps["egress_denied"]["severity"] == "low" and len(tb["title"]) <= 240
    assert nl_api.publish_incidents()["skipped"] if not nl_api.enabled() else True


def test_harness_survives_odd_commands_and_finds_heredoc_drivers(tmp=Path(__file__).parent / "_tmp_drv"):
    import json as _json
    import shutil
    from paperci.stages.builder import _pip_packages
    from paperci.stages.verifier import code_path_used, with_drivers
    # An unbalanced quote on a pip line crashed four evolution builds (neatlogs: ValueError: No closing quotation).
    assert _pip_packages("pip install numpy 'scipy") == ["numpy"]
    assert _pip_packages('pip install "torch==2.3" tqdm') == ["torch", "tqdm"]
    # A driver written with a shell heredoc that imports the repo's module is running the repo's code.
    tmp.mkdir(exist_ok=True)
    try:
        tree = [{"path": "real_data_har.py"}, {"path": "README.md"}]
        script = "#!/bin/bash\ncd /work\npython3 /work/har_repro.py\n"
        assert not code_path_used(with_drivers(script, tmp), tree)
        cmd = ("cat > /work/har_repro.py <<'EOF'\nimport sys\nsys.path.insert(0, '/work/repo')\n"
               "import real_data_har as R\nR.run()\nEOF\npython3 /work/har_repro.py")
        (tmp / "commands.json").write_text(_json.dumps([cmd]), encoding="utf-8")
        assert code_path_used(with_drivers(script, tmp), tree)
        # The final /work snapshot wins over earlier versions of the same file.
        (tmp / "drivers.json").write_text(_json.dumps({"/work/har_repro.py": "print(0.5)\n"}), encoding="utf-8")
        assert not code_path_used(with_drivers(script, tmp), tree)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_sandbox_owner_label():
    from paperci.sandbox import owner_alive, owner_label
    assert owner_alive("") is None and owner_alive("123:?") is None
    if Path("/proc").is_dir():  # Linux (the server): a live owner, and a pid that cannot exist
        assert owner_alive(owner_label()) is True
        assert owner_alive("4194999:12345") is False
    else:
        assert owner_alive(owner_label()) is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
