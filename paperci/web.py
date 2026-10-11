"""PaperCI web app. An author submits a paper (arXiv link, or a file in any common academic format) plus its
code; the PaperCI agent reads it, talks with the author in a chat, rebuilds the environment in a fresh
sandbox, verifies every claim, asks the author for help when it hits something only they can fix, retries,
and delivers a private review. It can keep watching the repository and re-verify on new commits.

    python -m paperci.web            # http://127.0.0.1:8000  (binds localhost only)

One sandbox run at a time (real model spend). Runs are `python -m paperci review ...` subprocesses, so a
crash never takes the server down; the agent's conversation state lives in SQLite."""
from __future__ import annotations

import html
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import concierge, db, ledger, sources, tracing
from .config import REGISTRY_DIR, ROOT

WEB = REGISTRY_DIR / "web"
JOBS, OUTBOX, UPLOADS = WEB / "jobs", WEB / "outbox", WEB / "uploads"
for d in (JOBS, OUTBOX, UPLOADS):
    d.mkdir(parents=True, exist_ok=True)
BASE_URL = os.environ.get("PAPERCI_WEB_URL", "http://127.0.0.1:8000")
MAX_UPLOAD = 25 * 1024 * 1024
INTAKE_WAIT = int(os.environ.get("PAPERCI_WEB_INTAKE_WAIT", "600"))   # start anyway if the author doesn't answer
WATCH_EVERY = int(os.environ.get("PAPERCI_WEB_WATCH_EVERY", "900"))
MAX_ATTEMPTS = 3
ANSI = re.compile(r"\x1b\[[0-9;]*m")
EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,24}$")
LOCK = threading.Lock()

db.ex("""CREATE TABLE IF NOT EXISTS web_jobs (token TEXT PRIMARY KEY, email TEXT, kind TEXT, input TEXT, filename TEXT,
         repo TEXT, paper_id TEXT, title TEXT, label TEXT, status TEXT, phase TEXT, attempt INTEGER DEFAULT 0,
         guidance TEXT DEFAULT '[]', created REAL, started REAL, finished REAL, asked_at REAL, review_path TEXT,
         error TEXT, watch INTEGER DEFAULT 0, watch_sha TEXT, watch_checked REAL)""")
db.ex("""CREATE TABLE IF NOT EXISTS web_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT, role TEXT,
         kind TEXT, text TEXT, ts REAL)""")

STEPS = [
    ("Read the paper", "Find the paper text and its code", ["Fetch paper metadata", "Snapshot repository metadata"]),
    ("Check it can run here", "CPU sandbox, no GPU, no API keys", ["Triage"]),
    ("Seal the claims", "Numbers extracted with quotes, hashed, hidden from the engineer agent", ["Extract and seal"]),
    ("Talk to the author", "Plan, questions, your guidance", ["__chat__"]),
    ("Rebuild the environment", "Fresh container; installs deps, gets your code running", ["Builder agent"]),
    ("Verify with the network off", "A different model family reruns it in a new container", ["Independent verifier"]),
    ("Rerun with other seeds", "Calibrates a tolerance from the spread", ["Robustness"]),
    ("Compare claims vs measured", "Plus anti-cheat integrity checks", ["Verdict"]),
    ("Package a self-contained environment", "Script, environment and hidden test; replayed from scratch", ["Package as", "Oracle"]),
]
HOSTS = {"codeload.github.com": "GitHub", "github.com": "GitHub", "api.github.com": "GitHub API",
         "objects.githubusercontent.com": "GitHub releases", "release-assets.githubusercontent.com": "GitHub releases",
         "raw.githubusercontent.com": "GitHub raw", "pypi.org": "PyPI", "files.pythonhosted.org": "PyPI",
         "download.pytorch.org": "PyTorch wheels", "huggingface.co": "Hugging Face", "zenodo.org": "Zenodo",
         "www.openml.org": "OpenML", "archive.ics.uci.edu": "UCI datasets"}


# ---------------- state ----------------
def _job(token: str) -> dict | None:
    r = db.q1("SELECT * FROM web_jobs WHERE token=?", (token,))
    return dict(r) if r else None


def _set(token: str, **kw) -> None:
    db.ex(f"UPDATE web_jobs SET {','.join(k + '=?' for k in kw)} WHERE token=?", (*kw.values(), token))


def _say(token: str, text: str, role: str = "agent", kind: str = "msg") -> None:
    db.ex("INSERT INTO web_messages (token, role, kind, text, ts) VALUES (?,?,?,?,?)", (token, role, kind, text, time.time()))


def _messages(token: str) -> list[dict]:
    return [dict(r) for r in db.q("SELECT role, kind, text, ts FROM web_messages WHERE token=? ORDER BY id", (token,))]


def _guidance(job: dict) -> list[concierge.Guidance]:
    return [concierge.Guidance(**g) for g in json.loads(job["guidance"] or "[]")]


def _email(job: dict, subject: str, body: str) -> None:
    """Mail delivery is not connected to a provider in this build; messages are written to the outbox."""
    n = len(list(OUTBOX.glob(f"{job['token']}*.eml")))
    (OUTBOX / f"{job['token']}-{n}.eml").write_text(
        f"To: {job['email']}\nFrom: PaperCI <agent@paperci.local>\nSubject: {subject}\n\n{body}\n\n"
        f"Open the conversation: {BASE_URL}/job/{job['token']}\n", encoding="utf-8")


# ---------------- agent loop ----------------
def _cmd(job: dict, *extra: str) -> list[str]:
    cmd = [sys.executable, "-u", "-m", "paperci", "review", "--label", job["label"] + (f"-{job['attempt']}" if job["attempt"] > 1 else "")]
    cmd += (["--file", job["input"]] if job["kind"] == "file" else [job["input"]])
    if job["repo"]:
        cmd += ["--repo", job["repo"]]
    return cmd + list(extra)


def _run_label(job: dict) -> str:
    return job["label"] + (f"-{job['attempt']}" if job["attempt"] > 1 else "")


NARRATE = [  # (pattern on a live log line, what the agent tells the author, once-key)
    (r"fetched (\S+) @ (\w+) inside the sandbox", "Cloned `{0}` at `{1}` into a fresh, locked-down container. Getting it running now.", "fetch"),
    (r"repo fetch failed", "The first download attempt failed; checking my own infrastructure.", "fetchfail"),
    (r"self-heal: (.+); retrying", "Found and fixed a problem on my side ({0}). Retrying.", None),
    (r"\$ .*pip3? install (?:-\S+ )*-r (\S+).*-> exit 0", "Installed everything in your `{0}`.", "pipr"),
    (r"\$ .*pip3? install (?:-\S+ )*([A-Za-z][\w.\-\[\]=<>]*(?: [A-Za-z][\w.\-\[\]=<>]*)*).*-> exit 0", "Installed dependencies: `{0}`.", "pip"),
    (r"classified failure '(\w+)'", "Hit a `{0}` error. Applying a fix I learned on earlier papers.", "cls:{0}"),
    (r"learned controller fired", "I've read enough of the code; switching to actually running it.", "ctrl"),
    (r"wrote /work/paperci_run.sh", "Wrote a single run script that regenerates your numbers.", "script"),
    (r"dry run REJECTED", "My script failed with the network switched off, so it secretly depended on a download. Fixing it.", None),
    (r"dry run accepted", "Your code ran end to end with the network switched off.", "dry"),
    (r"using the author's guidance", "Following your guidance for this attempt.", "guid"),
    (r"Robustness: reruns with other seeds done.*exit codes \[([\d, ]+)\]", "Independent verifier reran it in fresh containers with no network: exit codes [{0}].", "rob"),
    (r"Oracle: replay .* reward ([\d.]+)", "Packaged a self-contained environment and replayed it from scratch (reward {0}).", "oracle"),
]


def _narrate(token: str, line: str, seen: set) -> None:
    for pat, msg, key in NARRATE:
        m = re.search(pat, line)
        if m:
            k = key.format(*m.groups()) if key else None
            if k and k in seen:
                return
            if k:
                seen.add(k)
            _say(token, msg.format(*[g.strip()[:80] for g in m.groups()]), kind="narration")
            return


def _subprocess(job: dict, cmd: list[str], log: Path, narrate: bool) -> tuple[int, str]:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PAPERCI_LIVE": "1"}
    seen: set = set()
    with open(log, "a", encoding="utf-8") as f:
        p = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                             text=True, encoding="utf-8", errors="replace", bufsize=1)
        for line in p.stdout:
            f.write(line)
            f.flush()
            if narrate and " - neatlogs - " not in line:
                _narrate(job["token"], ANSI.sub("", line), seen)
        p.wait()
    return p.returncode, log.read_text(encoding="utf-8", errors="replace")


def _prepare(job: dict) -> None:
    t = job["token"]
    _set(t, status="running", phase="prepare", started=job["started"] or time.time())
    rc, out = _subprocess(job, _cmd(job, "--prepare-only"), JOBS / f"{t}.log", narrate=False)
    m = re.findall(r"PREPARED: (\{.*\})", out)
    if not m:
        if "no public code repository" in out:
            _say(t, "I read your paper but couldn't find its code. **What's the GitHub URL of the repository?**", kind="question")
            _set(t, status="awaiting_author", phase="need_repo", asked_at=time.time())
            return
        _set(t, status="failed", error=_last_line(out), finished=time.time())
        _say(t, f"I couldn't read this submission: {_last_line(out)}")
        return
    prep = json.loads(m[-1])
    row = db.q1("SELECT title FROM papers WHERE arxiv_id=?", (prep["paper_id"],))
    _set(t, paper_id=prep["paper_id"], title=row["title"] if row else None)
    job = _job(t)
    it = concierge.intake(job["paper_id"], job["label"])
    text = it.message + ("\n\n" + "\n".join(f"{i}. {q}" for i, q in enumerate(it.questions, 1)) if it.questions else "")
    _say(t, text, kind="question" if it.questions else "msg")
    if prep.get("triage") != "accept" or not prep.get("n_claims"):
        _finish(job, run=False)
    elif it.questions:
        _say(t, f"_I'll start with my own plan in {INTAKE_WAIT // 60} minutes if I don't hear back._", kind="note")
        _set(t, status="awaiting_author", phase="intake", asked_at=time.time())
    else:
        _set(t, status="queued_run", phase="run")


def _run(job: dict) -> None:
    t = job["token"]
    attempt = job["attempt"] + 1
    _set(t, status="running", phase="run", attempt=attempt)
    job = _job(t)
    notes, focus = concierge.builder_notes(_guidance(job))
    extra = []
    if notes:
        hf = JOBS / f"{t}-hints-{attempt}.txt"
        hf.write_text(notes, encoding="utf-8")
        extra += ["--hints-file", str(hf)]
    if focus:
        extra += ["--focus", ",".join(focus)]
    if attempt > 1:
        _say(t, f"Starting attempt {attempt} with what you told me.", kind="narration")
    rc, out = _subprocess(job, _cmd(job, *extra), JOBS / f"{t}.log", narrate=True)
    paths = re.findall(r"REVIEW_PATH: (.+)", out)
    if rc != 0 or not paths:
        _set(t, status="failed", error=_last_line(out), finished=time.time())
        _say(t, f"Something on my side failed and I stopped: `{_last_line(out)[:200]}`. I've kept everything so far.")
        return
    _set(t, review_path=paths[-1].strip())
    q = concierge.blocked_question(job["paper_id"], _run_label(job)) if attempt < MAX_ATTEMPTS else None
    if q:
        _say(t, q, kind="question")
        _set(t, status="awaiting_author", phase="blocked", asked_at=time.time())
        _email(job, "PaperCI needs one thing from you", q)
        return
    _finish(_job(t), run=True)


def _finish(job: dict, run: bool) -> None:
    t = job["token"]
    if run:
        _say(t, concierge.summary(job["paper_id"], _run_label(job)))
        _say(t, "Want me to keep an eye on this? I can **watch your repository and re-verify on every new commit**.",
             kind="offer")
    elif not job["review_path"]:
        try:
            from .review import write
            _set(t, review_path=str(write(job["paper_id"], _run_label(job))))
        except Exception:
            pass
    _set(t, status="done", phase="done", finished=time.time())
    _email(_job(t), f"Your PaperCI review is ready: {job['title'] or job['input']}",
           "PaperCI finished verifying the claims of your paper. The review lists every headline claim, what your "
           "released code produced in a fresh sandbox, and concrete suggestions.")


def _last_line(out: str) -> str:
    return next((ANSI.sub("", l).strip() for l in reversed(out.splitlines())
                 if l.strip() and " - neatlogs - " not in l and not l.startswith("Traceback")), "failed")[-300:]


def on_reply(token: str, text: str) -> None:
    job = _job(token)
    _say(token, text, role="author")
    if job["phase"] == "need_repo":
        repo = sources.find_github(text)
        if not repo:
            _say(token, "That doesn't look like a GitHub URL. Could you paste the full `https://github.com/owner/repo` link?",
                 kind="question")
            return
        _set(token, repo=repo, status="queued_prepare", phase="prepare")
        _say(token, f"Thanks, using `{repo}`.")
        return
    if job["phase"] == "done":
        if re.search(r"\b(watch|yes|sure|please|ok)\b", text, re.I) and job["paper_id"]:
            _start_watch(job)
        else:
            _say(token, "Noted. Reply \"watch\" any time and I'll re-verify on every new commit.")
        return
    if job["phase"] not in ("intake", "blocked"):
        _say(token, "Got it, I'll take that into account at the next step.")
        _add_guidance(job, concierge.Guidance(notes=text[:1500]))
        return
    g = concierge.parse_reply(job["paper_id"], job["label"], _messages(token), text)
    if g.repo_url and g.repo_url != job["repo"]:
        _set(token, repo=g.repo_url)
    _add_guidance(job, g)
    _say(token, g.reply or "Thanks, starting now.")
    if g.finish and job["phase"] == "blocked":
        _finish(_job(token), run=True)
    elif g.go:
        _set(token, status="queued_run", phase="run")


def _add_guidance(job: dict, g: concierge.Guidance) -> None:
    gs = json.loads(_job(job["token"])["guidance"] or "[]") + [g.model_dump()]
    _set(job["token"], guidance=json.dumps(gs))


def _head_sha(repo: str) -> str | None:
    m = sources.GH_RE.match(repo or "")
    if not m:
        return None
    r = sources.gh_api(f"repos/{m.group(1)}/{m.group(2)}/commits/HEAD")
    return r.json().get("sha") if r.status_code == 200 else None


def _start_watch(job: dict) -> None:
    repo = job["repo"] or (db.q1("SELECT repo_url FROM papers WHERE arxiv_id=?", (job["paper_id"],)) or {})["repo_url"]
    sha = _head_sha(repo)
    _set(job["token"], watch=1, watch_sha=sha, watch_checked=time.time(), repo=repo)
    _say(job["token"], f"Watching `{repo}` (now at `{(sha or '?')[:10]}`). When you push a new commit I'll rebuild "
                       "from scratch, re-verify every claim, and post the new review here.")


def _check_watches() -> None:
    for j in db.q("SELECT * FROM web_jobs WHERE watch=1 AND status='done' AND (watch_checked IS NULL OR watch_checked<?)",
                  (time.time() - WATCH_EVERY,)):
        j = dict(j)
        sha = _head_sha(j["repo"])
        _set(j["token"], watch_checked=time.time())
        if sha and j["watch_sha"] and sha != j["watch_sha"]:
            # A new commit: re-snapshot the code (claims stay sealed from the same paper) and re-verify.
            db.ex("DELETE FROM stages WHERE arxiv_id=? AND run_label='shared' AND stage IN ('snapshot','triage')", (j["paper_id"],))
            _set(j["token"], watch_sha=sha, status="queued_prepare", phase="prepare", attempt=0, review_path=None,
                 label=f"web-{j['token'][:6]}-{sha[:7]}")
            _say(j["token"], f"New commit `{sha[:10]}` on `{j['repo']}`. Re-verifying from scratch.", kind="narration")


def _worker() -> None:
    while True:
        row = None
        try:
            with LOCK:
                row = db.q1("SELECT * FROM web_jobs WHERE status IN ('queued_prepare','queued_run') ORDER BY created LIMIT 1")
            if row:
                job = dict(row)
                (_prepare if job["status"] == "queued_prepare" else _run)(job)
                continue
            for j in db.q("SELECT token FROM web_jobs WHERE status='awaiting_author' AND phase='intake' AND asked_at<?",
                          (time.time() - INTAKE_WAIT,)):
                _say(j["token"], "I haven't heard back, so I'm starting with my own plan. You can still reply; I'll use it "
                                 "if I need another attempt.")
                _set(j["token"], status="queued_run", phase="run")
            _check_watches()
        except Exception as e:  # keep the agent alive; surface the error in the conversation
            if row:
                _set(row["token"], status="failed", error=str(e)[:300], finished=time.time())
                _say(row["token"], f"I hit an internal error and stopped: `{str(e)[:200]}`")
        time.sleep(2)


# ---------------- progress for the UI ----------------
def _progress(job: dict) -> dict:
    log = JOBS / f"{job['token']}.log"
    raw = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
    # Each attempt is a new subprocess that starts with this header; show the latest attempt's progress.
    seg = raw[raw.rfind("PaperCI  ·  reviewing"):] if "PaperCI  ·  reviewing" in raw else raw
    lines = [ANSI.sub("", l) for l in seg.splitlines() if " - neatlogs - " not in l and l.strip()]
    state = {s[0]: "todo" for s in STEPS}
    detail: dict[str, str] = {}
    feed = []
    for l in lines:
        m = re.match(r"^(\d\d:\d\d) (\s*)([>+x!~.=]) (.*)$", l)
        if not m:
            continue
        ts, ind, g, txt = m.groups()
        if ind:
            feed.append({"t": ts, "g": g, "text": txt.strip()})
            continue
        for name, _, keys in STEPS:
            if any(txt.startswith(k) for k in keys):
                if g == ">":
                    state[name] = "run" if state[name] != "ok" or name.startswith("Package") else state[name]
                elif g in "+=":
                    state[name] = "ok"
                    if "->" in txt:
                        detail[name] = txt.split("->", 1)[1].strip()
                elif g == "x":
                    state[name] = "fail"
    msgs = _messages(job["token"])
    if any(m["role"] == "agent" and m["kind"] in ("question", "msg") for m in msgs):
        state["Talk to the author"] = "wait" if job["status"] == "awaiting_author" and job["phase"] == "intake" else "ok"
    if detail.get("Check it can run here", "").startswith("reject"):
        state["Check it can run here"] = "fail"
    for k in ("Verify with the network off", "Rerun with other seeds"):
        if detail.get(k) == "exit codes []":
            state[k], detail[k] = "skip", "skipped: nothing to run yet"
    if detail.get("Compare claims vs measured", "").startswith(("blocked", "inconclusive")):
        state["Compare claims vs measured"] = "warn"
    b = detail.get("Rebuild the environment", "")
    if b and "built" not in b:
        state["Rebuild the environment"] = "fail" if job["status"] != "awaiting_author" else "wait"
    if job["status"] in ("done", "failed"):
        for k, v in state.items():
            if v == "run":
                state[k] = "fail" if job["status"] == "failed" else "ok"
            elif v == "todo":
                state[k] = "skip"
    claims = None
    mc = re.search(r"(\d+) claims sealed", "\n".join(lines))
    if mc:
        claims = int(mc.group(1))
    elif job["paper_id"]:
        r = db.get_stage(job["paper_id"], "claims", "shared")
        if r and r["output"]:
            claims = json.loads(r["output"]).get("n_claims")
    return {"status": job["status"], "phase": job["phase"], "title": job["title"], "attempt": job["attempt"],
            "steps": [{"name": n, "why": w, "state": state[n], "detail": detail.get(n, "")} for n, w, _ in STEPS],
            "feed": feed[-80:], "claims_sealed": claims, "connections": _connections(job, raw), "messages": msgs,
            "elapsed": int((job["finished"] or time.time()) - (job["started"] or job["created"])),
            "review": f"/job/{job['token']}/review" if job["review_path"] else None, "error": job["error"],
            "watching": bool(job["watch"]), "queued_ahead": db.q1(
                "SELECT COUNT(*) n FROM web_jobs WHERE status IN ('queued_prepare','queued_run','running') AND created<?",
                (job["created"],))["n"] if job["status"].startswith("queued") else 0}


def _connections(job: dict, raw: str) -> list[dict]:
    out: dict[str, str] = {}
    if job["kind"] == "arxiv":
        out["arXiv"] = "ok"
    if "fetched " in raw:
        out["GitHub"] = "ok"
    if re.search(r"pip3? install[^\n]*-> exit 0", raw):
        out["PyPI"] = "ok"
    if job["paper_id"]:
        for r in db.q("SELECT output FROM stages WHERE arxiv_id=? AND stage='build' AND run_label LIKE ? AND status='done'",
                      (job["paper_id"], job["label"] + "%")):
            eg = json.loads(r["output"]).get("egress") or {}
            for h in eg.get("allowed", {}):
                out[HOSTS.get(h, h)] = "ok"
            for h in eg.get("denied", {}):
                out.setdefault(HOSTS.get(h, h), "blocked")
    if "--network none" in raw:
        out["Verification: network OFF"] = "off"
    return [{"host": k, "state": v} for k, v in out.items()]


# ---------------- pages ----------------
CSS = (Path(__file__).with_name("web.css")).read_text(encoding="utf-8")


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>{CSS}</style></head><body>
<div class=nav><a class=logo href="/">Paper<span>CI</span></a><div><a class=l href="/#how">How it works</a>
<a class=l href="/examples/index.html">Example reviews</a><a class=l href="https://github.com/isHeSatoshi/paperci">GitHub</a></div></div>
{body}</body></html>""")


def landing(error: str = "", email: str = "", arxiv: str = "", repo: str = "") -> HTMLResponse:
    e = html.escape
    err = f"<div class=err>{e(error)}</div>" if error else ""
    return _page("PaperCI - verify your paper's claims before you publish", f"""
<div class=hero><div class="wrap grid"><div>
<span class=kick>CodeRabbit for research papers</span>
<h1>Your paper says it. <em>Does your code?</em></h1>
<p class=lead>Send us your paper and its code. The PaperCI agent reads it, talks with you about what matters,
rebuilds your environment from scratch in a locked-down sandbox, re-runs your experiments, and checks every
headline claim against what your code actually produces, before reviewers or readers do.</p>
<ul class=ticks><li>An agent, not a form: it asks you only what it needs (data mirror, entry point, which claims matter) and comes back to you when it's stuck</li>
<li>Claims are sealed first: the engineer agent never sees the numbers it has to hit</li>
<li>Verified by a different AI model family, three reruns, network switched off</li>
<li>Catches shortcuts: hardcoded answers, copied or replayed results, scripts that skip your code</li>
<li>Private claim-by-claim review, a self-contained environment anyone can replay, and re-verification on every new commit</li></ul>
</div>
<form class=card method=post action="/submit" enctype="multipart/form-data" onsubmit="document.getElementById('sb').textContent='Starting the agent...'">
<h2>Verify my paper</h2><div class=muted>Private by default. Usually 10-20 minutes for CPU-scale code.</div>
<label>Your email</label><input type=email name=email required placeholder="you@university.edu" value="{e(email)}">
<label>Your paper</label>
<div class=tabs><button type=button id=ta class=on onclick="tab('a')">arXiv link</button><button type=button id=tp onclick="tab('p')">Upload a file</button></div>
<div id=pa><input type=text name=arxiv placeholder="https://arxiv.org/abs/2610.01234" value="{e(arxiv)}" style="margin-top:10px"></div>
<div id=pp style="display:none;margin-top:10px"><label class=drop id=dz><input type=file name=paper accept=".pdf,.tex,.zip,.tar,.gz,.tgz,.docx,.md,.txt,.html,.htm" style="display:none" onchange="picked(this)">
<span id=dzt>Drop your paper here or click to choose<br><span class=muted>PDF, LaTeX (.tex or source .zip/.tar.gz), Word .docx, HTML, Markdown, text · max 25 MB</span></span></label></div>
<label>Code repository</label><input type=url name=repo placeholder="https://github.com/you/your-paper-code (found automatically if linked)" value="{e(repo)}">
<label class=chk><input type=checkbox name=consent required> I am an author (or have permission) and agree that PaperCI runs this public code in an isolated sandbox.</label>
<button class=btn id=sb>Start verification</button>{err}
<div class=muted style="margin-top:10px">Today: CPU-scale code, no GPU, no paid API keys. GPU and simulator workloads are on the roadmap.</div>
</form></div></div>
<div class=wrap id=how><div class=how><h3>What the agent does with your paper</h3><div class=cols>
<div class=step><div class=num>1</div><b>Reads and seals</b><span class=muted>Extracts your headline claims with verbatim quotes and seals them (sha256). The engineer agent only sees metric names.</span></div>
<div class=step><div class=num>2</div><b>Talks with you</b><span class=muted>Shares what it found and its plan, asks the 1-3 questions that change the plan, and works with your answers.</span></div>
<div class=step><div class=num>3</div><b>Rebuilds and verifies</b><span class=muted>Fresh non-root container, dependencies and data from GitHub, PyPI, Hugging Face, Zenodo. Then three offline reruns judged by a different model family.</span></div>
<div class=step><div class=num>4</div><b>Reviews and keeps watching</b><span class=muted>Claim-by-claim review, fixes, badge and a replayable environment. Asks you when it's stuck; re-verifies on new commits.</span></div>
</div></div><div class=foot>PaperCI · built for neatHack 2026 · traces by neatlogs · sessions by Entire</div></div>
<script>
function tab(t){{document.getElementById('pa').style.display=t=='a'?'':'none';document.getElementById('pp').style.display=t=='p'?'':'none';
document.getElementById('ta').className=t=='a'?'on':'';document.getElementById('tp').className=t=='p'?'on':'';}}
function picked(i){{const d=document.getElementById('dz');if(i.files.length){{d.className='drop has';document.getElementById('dzt').textContent=i.files[0].name;}}}}
</script>""")


def job_page(token: str) -> HTMLResponse:
    job = _job(token)
    if not job:
        raise HTTPException(404)
    e = html.escape
    return _page(f"PaperCI - {job['title'] or 'verification'}", f"""
<div class=wrap><div style="padding-top:18px"><span class="pill {e(job['status'])}" id=pill>{e(job['status'])}</span>
<span class=muted id=meta style="margin-left:8px"></span>
<h1 style="font-size:28px;margin:10px 0 0" id=ttl>{e(job['title'] or job['input'])}</h1>
<div class=muted>Private conversation for {e(job['email'])} · bookmark this page to come back</div></div>
<div class=jobgrid>
<div><div class="box chat"><h4>PaperCI agent</h4><div id=msgs class=msgs></div>
<form id=rf class=reply onsubmit="send();return false"><textarea id=rt rows=2 placeholder="Reply to the agent..."></textarea>
<div class=qr id=qr></div><button class=btn2>Send</button></form></div></div>
<div><div class=box style="display:flex;gap:28px"><div><div class=big id=cl>-</div><div class=muted>claims sealed</div></div>
<div><div class=big id=el>0:00</div><div class=muted>elapsed</div></div><div><div class=big id=tn>0</div><div class=muted>agent actions</div></div>
<div><div class=big id=at>-</div><div class=muted>attempt</div></div></div>
<div class=box><h4>Progress</h4><div id=steps></div></div>
<div class=box><h4>Connections</h4><div class=chips id=conns><span class=muted>waiting...</span></div>
<div class=muted style="margin-top:10px">Builds can only reach an allowlist of code and data hosts. Verification runs with no network at all.</div></div>
<details class=box><summary><b>Live sandbox log</b></summary><div class=term id=feed><span class=t>waiting for the sandbox...</span></div></details>
</div></div><div id=rev class=wrap style="padding:0 0 60px"></div></div>
<script>
const G={{'+':'ok','!':'warn','x':'fail','~':'heal','>':'','.':'','=':''}};
function esc(s){{return (s||'').replace(/[&<>]/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;'}}[c]))}}
function md(s){{s=esc(s);s=s.replace(/\\*\\*(.+?)\\*\\*/g,'<b>$1</b>').replace(/`([^`]+)`/g,'<code>$1</code>').replace(/(^|\\s)_(.+?)_(\\s|$)/g,'$1<i>$2</i>$3');
return s.split('\\n').map(l=>/^\\s*(\\d+\\.|-)\\s/.test(l)?`<div class=li>${{l}}</div>`:l).join('<br>').replace(/<br><div/g,'<div').replace(/<\\/div><br>/g,'</div>');}}
let shown=false,nmsg=-1,busy=false;
document.addEventListener('keydown',e=>{{if(e.target.id=='rt'&&e.key=='Enter'&&!e.shiftKey){{e.preventDefault();send();}}}});
async function send(t){{const v=t||document.getElementById('rt').value.trim();if(!v||busy)return false;busy=true;
await fetch('/api/job/{token}/reply',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{text:v}})}});
document.getElementById('rt').value='';busy=false;tick(true);return false;}}
async function tick(once){{const r=await fetch('/api/job/{token}');const j=await r.json();
const P=document.getElementById('pill');P.className='pill '+j.status;P.textContent=j.status.replace('_',' ');
document.getElementById('meta').textContent=j.status.startsWith('queued')&&j.queued_ahead?(j.queued_ahead+' run(s) ahead of you'):(j.watching?'watching your repository':'');
if(j.title)document.getElementById('ttl').textContent=j.title;
if(j.messages.length!=nmsg){{nmsg=j.messages.length;const M=document.getElementById('msgs');
M.innerHTML=j.messages.map(m=>m.role=='author'?`<div class="m me"><div class=b>${{md(m.text)}}</div></div>`:
`<div class="m ${{m.kind}}"><div class=av>PCI</div><div class=b>${{md(m.text)}}</div></div>`).join('')+(j.status=='running'?'<div class="m typing"><div class=av>PCI</div><div class=b><span></span><span></span><span></span></div></div>':'');
M.scrollTop=M.scrollHeight;}}
const qr=document.getElementById('qr');qr.innerHTML=j.status=='awaiting_author'&&j.phase=='intake'?'<button type=button onclick="send(\\'Go ahead with your plan.\\')">Go ahead with your plan</button>':
(j.phase=='done'&&!j.watching&&j.review?'<button type=button onclick="send(\\'Yes, watch my repo.\\')">Yes, watch my repo</button>':'');
document.getElementById('steps').innerHTML=j.steps.map(s=>`<div class="st ${{s.state}}"><div class=dot>${{s.state=='ok'?'&#10003;':s.state=='fail'?'!':s.state=='wait'?'?':s.state=='warn'?'~':''}}</div><div><div class=n>${{s.name}}</div><div class=w>${{s.why}}</div>${{s.detail?`<div class=d>${{esc(s.detail)}}</div>`:''}}</div></div>`).join('');
if(j.connections.length)document.getElementById('conns').innerHTML=j.connections.map(c=>`<span class="chip ${{c.state}}">${{c.state=='blocked'?'&#10007; ':c.state=='off'?'':'&#10003; '}}${{esc(c.host)}}</span>`).join('');
document.getElementById('cl').textContent=j.claims_sealed??'-';const m=Math.floor(j.elapsed/60),s=j.elapsed%60;document.getElementById('el').textContent=m+':'+String(s).padStart(2,'0');
document.getElementById('tn').textContent=j.feed.length;document.getElementById('at').textContent=j.attempt||'-';
const f=document.getElementById('feed');if(j.feed.length){{f.innerHTML=j.feed.map(x=>`<div><span class=t>${{x.t}}</span><span class="g ${{G[x.g]||''}}">${{x.g}}</span>${{esc(x.text)}}</div>`).join('');f.scrollTop=f.scrollHeight;}}
if(j.review&&j.status=='done'&&!shown){{shown=true;document.getElementById('rev').innerHTML=`<div class=box style="border-color:#bbf7d0;background:#f0fdf4"><b>Your review</b> · <a href="${{j.review}}" target=_blank>open full page</a> · <a href="${{j.review}}.md" target=_blank>Markdown</a> · <a href="${{j.review}}.svg" target=_blank>badge</a></div><iframe src="${{j.review}}"></iframe>`;}}
if(RED)redact();
if(!once)setTimeout(tick,(j.status=='done'||j.status=='failed')?15000:2000);}}
// ?redact=1: hide the paper's identity for screenshots and demos (title, repo names, detailed messages)
const RED=location.search.includes('redact');
function redact(){{document.getElementById('ttl').textContent='Your paper';document.title='PaperCI';
document.querySelectorAll('.m.question .b, .m.msg .b').forEach(b=>{{if(!b.textContent.startsWith("Hi! I'm the PaperCI agent"))b.style.filter='blur(3.5px)';}});
document.querySelectorAll('code').forEach(c=>{{if(/github\\.com|^[\\w.-]+\\/[\\w.-]+$/.test(c.textContent))c.textContent='author/repo';}});
const w=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);let n;while((n=w.nextNode())){{n.nodeValue=n.nodeValue.replace(/[\\w.+-]+@[\\w.-]+\\.[a-z]{{2,}}/gi,'you@university.edu');}}
const rv=document.getElementById('rev');if(rv)rv.style.display='none';}}
tick();
</script>""")


app = FastAPI(title="PaperCI", docs_url=None, redoc_url=None)
if (ROOT / "docs" / "reviews").exists():
    app.mount("/examples", StaticFiles(directory=ROOT / "docs" / "reviews", html=True), name="examples")


@app.get("/", response_class=HTMLResponse)
def index():
    return landing()


@app.post("/submit")
async def submit(email: str = Form(...), arxiv: str = Form(""), repo: str = Form(""), consent: str = Form(""),
                 paper: UploadFile | None = File(None)):
    email, arxiv, repo = email.strip(), arxiv.strip(), repo.strip()
    if not EMAIL.match(email):
        return landing("Please enter a valid email address.", email, arxiv, repo)
    if not consent:
        return landing("Please confirm you may submit this paper.", email, arxiv, repo)
    if repo and not sources.find_github(repo):
        return landing("The code repository must be a public github.com URL.", email, arxiv, repo)
    if ledger.local_total() > float(os.environ.get("PAPERCI_WEB_SPEND_CAP", "15")):
        return landing("PaperCI has reached its spending limit for this demo. Please try again later.", email, arxiv, repo)
    token = secrets.token_urlsafe(12)
    data = await paper.read(MAX_UPLOAD + 1) if paper is not None and paper.filename else b""
    if data:
        if len(data) > MAX_UPLOAD:
            return landing("Please upload a file under 25 MB.", email, arxiv, repo)
        fname = re.sub(r"[^A-Za-z0-9._-]", "_", Path(paper.filename).name)[-80:]
        path = UPLOADS / f"{token}-{fname}"
        path.write_bytes(data)
        try:
            text, _, fmt = sources.paper_text(data, fname)
            if len(text) < 2000:
                raise ValueError(f"only {len(text)} characters of text")
        except Exception as ex:
            return landing(f"Could not read that file: {ex}", email, arxiv, repo)
        kind, inp, pid = "file", str(path), sources.upload_id(data)
    elif arxiv:
        try:
            pid = sources.parse_arxiv_id(arxiv)
        except ValueError:
            return landing("That does not look like an arXiv link or id.", email, arxiv, repo)
        kind, inp, fname = "arxiv", pid, None
    else:
        return landing("Add an arXiv link or upload your paper.", email, arxiv, repo)
    row = db.q1("SELECT title FROM papers WHERE arxiv_id=?", (pid,))
    db.ex("INSERT INTO web_jobs (token,email,kind,input,filename,repo,paper_id,title,label,status,phase,created) "
          "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (token, email, kind, inp, fname, repo or None, None,
                                               row["title"] if row else None, f"web-{token[:8]}", "queued_prepare",
                                               "prepare", time.time()))
    _say(token, "Hi! I'm the PaperCI agent. I'm reading your paper and looking at its code now; I'll tell you what I "
                "find and what I plan to check in a minute.", kind="msg")
    return RedirectResponse(f"/job/{token}", status_code=303)


@app.get("/job/{token}", response_class=HTMLResponse)
def job(token: str):
    return job_page(token)


@app.get("/api/job/{token}")
def api_job(token: str):
    j = _job(token)
    if not j:
        raise HTTPException(404)
    return JSONResponse(_progress(j))


@app.post("/api/job/{token}/reply")
async def api_reply(token: str, body: dict):
    if not _job(token):
        raise HTTPException(404)
    text = str(body.get("text", "")).strip()[:4000]
    if text:
        threading.Thread(target=on_reply, args=(token, text), daemon=True).start()
    return {"ok": True}


def _review_file(token: str, ext: str) -> Path:
    j = _job(token)
    if not j or not j["review_path"]:
        raise HTTPException(404)
    p = Path(j["review_path"]).with_suffix(ext)
    if not p.exists() or REGISTRY_DIR.resolve() not in p.resolve().parents:
        raise HTTPException(404)
    return p


@app.get("/job/{token}/review")
def review_html(token: str):
    return FileResponse(_review_file(token, ".html"), media_type="text/html")


@app.get("/job/{token}/review.md")
def review_md(token: str):
    return FileResponse(_review_file(token, ".md"), media_type="text/markdown; charset=utf-8")


@app.get("/job/{token}/review.svg")
def review_svg(token: str):
    return FileResponse(_review_file(token, ".svg"), media_type="image/svg+xml")


def main() -> None:
    import uvicorn
    tracing.init("web", "-")
    db.ex("UPDATE web_jobs SET status='queued_'||phase WHERE status='running' AND phase IN ('prepare','run')")
    threading.Thread(target=_worker, daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PAPERCI_WEB_PORT", "8000")), log_level="warning")


if __name__ == "__main__":
    main()
