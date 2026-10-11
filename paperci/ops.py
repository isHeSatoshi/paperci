"""Always-on operations. In the cloud PaperCI runs itself:

  autopilot  every PAPERCI_CYCLE_EVERY s: watch arXiv -> triage + seal new papers -> reproduce the freshest one
             -> write its review (private + anonymized). Runs inside the web worker, so only one sandbox job runs
             at a time and author submissions always go first.
  nightly    self-improvement from the day's dev-split failures (gated playbook generation), a chaos drill,
             and an anonymized daily digest.
  watchdog   (systemd timer, root) health checks with automatic repair: web app, Docker daemon, egress proxy,
             dead sandboxes, disk pressure. Every repair is an incident on the public board.

Everything is logged to registry/ops/events.jsonl. Public text never names a paper (anon ids only).

    python -m paperci.ops watchdog | nightly | chaos | digest | events
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .config import REGISTRY_DIR, ROOT

OPS = REGISTRY_DIR / "ops"
OPS.mkdir(parents=True, exist_ok=True)
EVENTS = OPS / "events.jsonl"
STATE = OPS / "state.json"
WATCH_STATE = OPS / "watchdog.json"
HEARTBEAT = OPS / "heartbeat.json"
LAST_GOOD = OPS / "last_good_commit"
# Hard wall-clock limits per worker job. A job past its limit is killed; a worker past limit + grace is "hung".
LIMITS = {"prepare": 30 * 60, "run": 2 * 3600, "cycle": 2 * 3600, "nightly": 3600}
GRACE = 600
IDLE_BEAT_MAX = 180
CYCLE_EVERY = int(os.getenv("PAPERCI_CYCLE_EVERY", "5400"))
NIGHTLY_HOUR = int(os.getenv("PAPERCI_NIGHTLY_HOUR", "3"))  # local hour (PAPERCI_TZ_OFFSET_H, default IST)
TZ_OFF = float(os.getenv("PAPERCI_TZ_OFFSET_H", "5.5")) * 3600
WEB_PORT = int(os.getenv("PAPERCI_WEB_PORT", "8000"))
DISK_MIN_GB = float(os.getenv("PAPERCI_DISK_MIN_GB", "10"))


def _local(ts: float | None = None) -> time.struct_time:
    return time.gmtime((ts or time.time()) + TZ_OFF)


def _owned(p: Path) -> None:
    """The watchdog runs as root; keep files writable by the service user that owns the registry."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        st = REGISTRY_DIR.stat()
        try:
            os.chown(p, st.st_uid, st.st_gid)
        except OSError:
            pass


def event(kind: str, text: str, **data) -> dict:
    e = {"ts": round(time.time(), 1), "kind": kind, "text": text[:400], **data}
    with open(EVENTS, "a", encoding="utf-8") as f:
        f.write(json.dumps(e, default=str) + "\n")
    _owned(EVENTS)
    return e


def events(n: int = 200, kinds: set[str] | None = None) -> list[dict]:
    if not EVENTS.exists():
        return []
    out = []
    for line in EVENTS.read_text(encoding="utf-8", errors="replace").splitlines()[-5000:]:
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not kinds or e["kind"] in kinds:
            out.append(e)
    return out[-n:]


def _load(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(p: Path, d: dict) -> None:
    p.write_text(json.dumps(d, indent=1), encoding="utf-8")
    _owned(p)


# ---------------------------------------------------------------- heartbeat (worker liveness, read by /healthz + watchdog)
def beat(busy: str | None = None) -> None:
    d = {"ts": time.time(), "pid": os.getpid(), "busy": busy,
         "deadline": time.time() + LIMITS.get(busy, 3600) if busy else None}
    tmp = HEARTBEAT.with_suffix(".tmp")
    tmp.write_text(json.dumps(d), encoding="utf-8")
    os.replace(tmp, HEARTBEAT)


def busy() -> bool:
    d = _load(HEARTBEAT)
    return bool(d.get("busy")) and time.time() < (d.get("deadline") or 0) + GRACE


def health() -> tuple[bool, dict]:
    """The web app is only healthy if its worker thread is alive: idle and beating, or busy within its deadline."""
    d, now = _load(HEARTBEAT), time.time()
    wd = _load(WATCH_STATE)
    info = {"watchdog_age_s": round(now - wd["last_run"]) if wd.get("last_run") else None}
    if not d:
        return True, {**info, "worker": "starting"}
    if d.get("busy"):
        overdue = now > d["deadline"] + GRACE
        return not overdue, {**info, "worker": "busy", "job": d["busy"], "busy_for_s": round(now - d["ts"]), "overdue": overdue}
    age = now - d["ts"]
    return age < IDLE_BEAT_MAX, {**info, "worker": "idle", "beat_age_s": round(age)}


# ---------------------------------------------------------------- autopilot (runs inside the web worker)
def enabled() -> bool:
    return os.getenv("PAPERCI_AUTOPILOT") == "1"


def due() -> str | None:
    if not enabled():
        return None
    from . import ledger
    st = _load(STATE)
    now = time.time()
    today = time.strftime("%Y-%m-%d", _local(now))
    cap = ledger.daily_cap()
    if cap is not None and ledger.today_total() >= cap * 0.9:
        if st.get("paused_day") != today:
            st["paused_day"] = today
            _save(STATE, st)
            event("budget", f"Daily LLM budget reached (${ledger.today_total():.2f} of ${cap:.2f}); autopilot paused "
                            "until midnight. Author submissions still run.")
        return None
    if st.get("nightly_day") != today and _local(now).tm_hour >= NIGHTLY_HOUR:
        return "nightly"
    if now - st.get("last_cycle", 0) >= CYCLE_EVERY:
        return "cycle"
    return None


def run_due(kind: str) -> None:
    # The schedule is advanced BEFORE the work starts: a job that crashes or hangs is skipped until its next
    # slot instead of being retried in a loop (no poison jobs).
    st = _load(STATE)
    beat(kind)
    try:
        if kind == "nightly":
            st["nightly_day"] = time.strftime("%Y-%m-%d", _local())
            _save(STATE, st)
            _sub([sys.executable, "-u", "-m", "paperci.ops", "nightly"], "nightly", LIMITS["nightly"])
        else:
            st["last_cycle"] = time.time()
            _save(STATE, st)
            cycle()
    finally:
        beat()


def _sub(cmd: list[str], name: str, timeout: int = LIMITS["cycle"]) -> tuple[int, str]:
    log = OPS / f"{name}-{time.strftime('%Y%m%d-%H%M', _local())}.log"
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    p = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                         errors="replace", env=env, start_new_session=os.name != "nt")
    try:
        out, _ = p.communicate(timeout=timeout)
        rc = p.returncode
    except subprocess.TimeoutExpired:
        kill_tree(p)
        out, rc = (p.communicate()[0] or ""), 124
        event("incident", f"Autopilot job '{name}' exceeded its {timeout // 60} min limit and was killed; "
                          "the schedule moved on", healed=True, component="worker")
    log.write_text(out, encoding="utf-8")
    return rc, out


def kill_tree(p: subprocess.Popen) -> None:
    """Kill a job and everything it started (docker CLI children included); its sandboxes are reaped by the
    watchdog's orphan sweep."""
    try:
        if os.name != "nt":
            import signal
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        else:
            p.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass


def cycle() -> dict:
    from . import review
    label = "live-" + time.strftime("%Y%m%d", _local())
    event("cycle", "Autopilot cycle started: checking arXiv for new papers with code")
    rc, out = _sub([sys.executable, "-u", "-m", "paperci", "cycle", "--prepare", os.getenv("PAPERCI_CYCLE_PREPARE", "6"),
                    "--reproduce", os.getenv("PAPERCI_CYCLE_REPRODUCE", "1"), "--label", label, "--fresh"], "cycle")
    w = re.search(r"watch: (\{.*\})", out)
    found = json.loads(w.group(1)) if w else {}
    prepared = re.findall(r"^prepare (\S+) (\{.*\})$", out, re.M)
    acc = sum(1 for _, j in prepared if json.loads(j).get("triage") == "accept")
    claims = sum(json.loads(j).get("n_claims") or 0 for _, j in prepared)
    summary = {"new_papers": found.get("new", 0), "triaged": len(prepared), "accepted": acc, "claims_sealed": claims,
               "reproduced": []}
    for pid, j in re.findall(r"^reproduce (\S+) (\{.*\})$", out, re.M):
        try:
            review.write(pid, label)
            r = review.build_review(pid, label, anonymize=True)
            review.write(pid, label, anonymize=True)
            summary["reproduced"].append(review.anon_id(pid))
            event("review", f"Reviewed paper {review.anon_id(pid)}: {r['headline']}", paper=review.anon_id(pid),
                  state=r["state"], claims=len(r["claims"]), cost=r["cost_usd"],
                  page=f"{review.anon_id(pid)}-{label}.html")
        except Exception as ex:
            event("error", f"Review rendering failed for {review.anon_id(pid)}: {type(ex).__name__}")
    stop = re.search(r"(BUDGET STOP|DISK STOP): (.*)", out)
    event("cycle", f"Autopilot cycle done: {summary['new_papers']} new papers, {len(prepared)} triaged "
                   f"({acc} runnable on CPU), {claims} claims sealed, {len(summary['reproduced'])} reproduced"
                   + (f"; stopped: {stop.group(1).lower()}" if stop else ""), rc=rc, **{k: v for k, v in summary.items() if k != "reproduced"})
    try:
        review.write_index(anonymize=True)
    except Exception:
        pass
    return summary


# ---------------------------------------------------------------- nightly
def nightly() -> dict:
    from . import tracing
    tracing.init("nightly", "-")
    res: dict = {}
    try:
        res["improve"] = self_improve()
    except Exception as ex:
        res["improve"] = {"error": f"{type(ex).__name__}: {str(ex)[:200]}"}
        event("error", f"Nightly self-improvement failed: {type(ex).__name__}")
    try:
        res["chaos"] = chaos_drill()
    except Exception as ex:
        res["chaos"] = {"error": str(ex)[:200]}
        event("error", f"Chaos drill failed to run: {type(ex).__name__}")
    res["digest"] = str(digest())
    tracing.shutdown()
    return res


def self_improve() -> dict:
    """Learn from the last two days of autonomous runs, dev split only (held-out papers never tune the builder)."""
    from . import db
    from .improver import improve
    days = [time.strftime("%Y%m%d", _local(time.time() - d * 86400)) for d in (0, 1)]
    labels = [f"live-{d}" for d in days if db.q1("SELECT 1 FROM stages WHERE run_label=? AND stage='build'", (f"live-{d}",))]
    if not labels:
        event("learn", "Nightly self-improvement: no new builds to learn from yet")
        return {"status": "nothing to learn"}
    out = improve(labels, apply=True, only_split="dev")
    acc = out.get("accepted", [])
    if out.get("generation_after"):
        names = ", ".join(a["rule"]["rule_id"] for a in acc[:6])
        event("learn", f"Learned playbook generation {out['generation_after']} from {sum(out.get('clusters', {}).values())} "
                       f"failures: {len(acc)} rule(s) passed the gate ({names}); {len(out.get('rejected', []))} rejected",
              generation=out["generation_after"], accepted=[a["rule"]["rule_id"] for a in acc],
              rejected=[{"rule": r["rule"]["rule_id"], "why": r["gate"]} for r in out.get("rejected", [])])
    else:
        event("learn", f"Nightly self-improvement: {len(out.get('rejected', []))} proposal(s), none passed the gate; "
                       "playbook unchanged", rejected=[{"rule": r["rule"]["rule_id"], "why": r["gate"]} for r in out.get("rejected", [])])
    return {k: out.get(k) for k in ("generation_before", "generation_after", "clusters")}


# ---------------------------------------------------------------- chaos drill
def _probe() -> tuple[bool, str]:
    """Can a sandbox on the internal network reach PyPI through the egress proxy?"""
    from .config import INTERNAL_NET, SANDBOX_IMAGE
    from .sandbox import PROXY_URL, _docker
    p = _docker("run", "--rm", "--network", INTERNAL_NET, "-e", f"HTTPS_PROXY={PROXY_URL}", "--entrypoint", "curl",
                SANDBOX_IMAGE, "-sS", "-m", "20", "-o", "/dev/null", "-w", "%{http_code}", "https://pypi.org/simple/pip/",
                check=False, timeout=90)
    out = (p.stdout + p.stderr).decode("utf-8", "replace")
    return p.returncode == 0 and out.strip().endswith("200"), out[-300:]


def chaos_drill(kind: str | None = None) -> dict:
    from .recovery import classify
    from .sandbox import PROXY_NAME, _docker, ensure_infra
    from .config import SANDBOX_IMAGE
    kind = kind or ("proxy_down" if _local().tm_yday % 2 == 0 else "dead_sandbox")
    t0 = time.time()
    if kind == "proxy_down":
        _docker("stop", "-t", "2", PROXY_NAME, check=False)
        ok, out = _probe()
        cls = classify(out, 1, "curl https://pypi.org") or "unclassified"
        healed = ensure_infra()
        ok2, _ = _probe()
        res = {"drill": kind, "detected": not ok, "class": cls, "actions": healed, "recovered": ok2,
               "mttr_s": round(time.time() - t0, 1)}
        event("chaos", f"Chaos drill (egress proxy killed): detected={'yes' if not ok else 'no'}, classified "
                       f"'{cls}', repaired ({'; '.join(healed) or 'no action'}), sandbox egress "
                       f"{'restored' if ok2 else 'NOT restored'} in {res['mttr_s']}s", **res)
    else:
        name = f"paperci-chaos-{int(t0)}"
        _docker("run", "--name", name, "--label", "paperci=1", "--entrypoint", "true", SANDBOX_IMAGE, check=False, timeout=120)
        healed = ensure_infra()
        gone = _docker("inspect", name, check=False).returncode != 0
        res = {"drill": kind, "actions": healed, "recovered": gone, "mttr_s": round(time.time() - t0, 1)}
        event("chaos", f"Chaos drill (orphaned sandbox left behind by a crash): "
                       f"{'cleaned up' if gone else 'NOT cleaned'} automatically ({'; '.join(healed) or 'no action'}) "
                       f"in {res['mttr_s']}s", **res)
    return res


# ---------------------------------------------------------------- watchdog (root, systemd timer)
def _sh(*cmd: str, timeout: int = 300) -> tuple[int, str]:
    try:
        p = subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr)[-500:]
    except Exception as ex:
        return 1, str(ex)


MAX_WEB_RESTARTS_PER_HOUR = 3


def _check_docker(st: dict, acts: list[str]) -> None:
    from .sandbox import docker_ok
    if docker_ok():
        return
    t0 = time.time()
    _sh("systemctl", "restart", "docker", timeout=180)
    ok = docker_ok()
    event("incident", f"Docker daemon was not responding; restarted it ({'healthy' if ok else 'STILL DOWN'})",
          healed=ok, component="docker", seconds=round(time.time() - t0, 1))
    acts.append("restarted docker")


def _check_sandbox_infra(st: dict, acts: list[str]) -> None:
    from .sandbox import ensure_infra
    for a in ensure_infra():
        if a.startswith("WARNING"):
            continue
        if not st.get("last_run"):  # first start on a fresh host is setup, not an incident
            event("boot", f"First start: sandbox infrastructure set up ({a})")
        else:
            event("incident", f"Sandbox infrastructure drift repaired: {a}", healed=True, component="sandbox")
        acts.append(a)


def _check_orphans(st: dict, acts: list[str]) -> None:
    """Running sandboxes nobody owns: any sandbox while the worker is idle, or one older than the longest job."""
    from .sandbox import PROXY_NAME
    rc, out = _sh("docker", "ps", "--filter", "label=paperci=1", "--format", "{{.ID}} {{.Names}} {{.CreatedAt}}")
    if rc != 0:
        return
    idle = not busy()
    victims = []
    for line in out.splitlines():
        parts = line.split(" ", 2)
        if len(parts) < 3 or parts[1] == PROXY_NAME:
            continue
        try:
            import calendar
            created = calendar.timegm(time.strptime(parts[2][:19], "%Y-%m-%d %H:%M:%S"))  # docker prints UTC
        except ValueError:
            continue
        age = time.time() - created
        if (idle and age > 300) or age > max(LIMITS.values()) + GRACE:
            victims.append(parts[0])
    if victims:
        _sh("docker", "rm", "-f", *victims, timeout=120)
        event("incident", f"Removed {len(victims)} orphaned sandbox(es) left running by a killed job", healed=True,
              component="sandbox")
        acts.append(f"removed {len(victims)} orphans")


def _check_disk(st: dict, acts: list[str]) -> None:
    free = shutil.disk_usage("/").free / 1e9
    st["disk_free_gb"] = round(free, 1)
    if free >= DISK_MIN_GB:
        return
    # Never delete what a running job needs: images used by running containers are kept, and the build
    # cache is only pruned while the worker is idle (a build may be using it).
    rc, used = _sh("docker", "ps", "--format", "{{.Image}}")
    in_use = set(used.split())
    if not busy():
        _sh("docker", "builder", "prune", "-af", timeout=600)
    _sh("docker", "image", "prune", "-f", timeout=600)
    rc, imgs = _sh("docker", "images", "--format", "{{.Repository}}:{{.Tag}}", "--filter", "reference=paperci/built*")
    old = [i for i in imgs.split() if i.startswith("paperci/built") and i not in in_use]
    if old and not busy():
        _sh("docker", "rmi", *old, timeout=600)  # no -f: docker itself refuses to remove an image a container uses
    after = shutil.disk_usage("/").free / 1e9
    event("incident", f"Disk pressure ({free:.1f} GB free); pruned unused build cache/images: {after:.1f} GB free now",
          healed=after >= DISK_MIN_GB, component="disk", freed_gb=round(after - free, 1))
    acts.append("pruned disk")


def _rollback() -> str | None:
    """Back to the last commit that passed deploy checks (deploy/deploy.sh records it)."""
    good = LAST_GOOD.read_text().strip() if LAST_GOOD.exists() else ""
    rc, head = _sh("sudo", "-u", "ubuntu", "git", "-C", str(ROOT), "rev-parse", "HEAD")
    if not good or head.strip() == good:
        return None
    rc, _ = _sh("sudo", "-u", "ubuntu", "git", "-C", str(ROOT), "reset", "-q", "--hard", good)
    return good if rc == 0 else None


def _check_web(st: dict, acts: list[str]) -> None:
    ok, info = _web_health()
    if ok:
        st["web_fail"] = 0
        return
    st["web_fail"] = st.get("web_fail", 0) + 1
    if st["web_fail"] < 2:
        return
    st["web_fail"] = 0
    recent = [t for t in st.get("web_restarts", []) if time.time() - t < 3600]
    why = info.get("error") or (f"worker {info.get('worker')} " + ("past its deadline" if info.get("overdue") else
                                                                    f"silent for {info.get('beat_age_s')}s"))
    if len(recent) >= MAX_WEB_RESTARTS_PER_HOUR:
        # Restarting again would only loop. Escalate once: roll back to the last known-good deploy.
        good = None if st.get("rolled_back_at", 0) > time.time() - 3600 else _rollback()
        if good:
            st["rolled_back_at"] = time.time()
            _sh("systemctl", "restart", "paperci-web", timeout=120)
            event("incident", f"Web app kept failing after {len(recent)} restarts in an hour ({why}); rolled back to "
                              f"last good commit {good[:8]} and restarted", healed=_wait_web(), component="web")
            acts.append("rolled back")
        elif not st.get("escalated_at") or time.time() - st["escalated_at"] > 3600:
            st["escalated_at"] = time.time()
            event("incident", f"Web app keeps failing ({why}) and no rollback is available; stopped restarting to avoid "
                              "a restart loop. Needs a human.", healed=False, component="web")
        st["web_restarts"] = recent
        return
    _sh("systemctl", "restart", "paperci-web", timeout=120)
    st["web_restarts"] = recent + [time.time()]
    healed = _wait_web()
    event("incident", f"Web app failed two health checks ({why}); restarted it ({'healthy' if healed else 'STILL DOWN'})",
          healed=healed, component="web")
    acts.append("restarted web")


def _web_health() -> tuple[bool, dict]:
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{WEB_PORT}/healthz", timeout=10) as r:
            return r.status == 200, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as ex:  # 503 = process up, worker hung
        try:
            return False, json.loads(ex.read() or b"{}")
        except Exception:
            return False, {"error": f"HTTP {ex.code}"}
    except Exception as ex:
        return False, {"error": f"no response ({type(ex).__name__})"}


def _wait_web(seconds: int = 60) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if _web_health()[0]:
            return True
        time.sleep(3)
    return False


def _housekeeping(st: dict, acts: list[str]) -> None:
    for p in OPS.glob("*.log"):
        if time.time() - p.stat().st_mtime > 3 * 86400:
            p.unlink(missing_ok=True)
    if EVENTS.exists() and EVENTS.stat().st_size > 5_000_000:
        keep = EVENTS.read_text(encoding="utf-8", errors="replace").splitlines()[-20000:]
        EVENTS.write_text("\n".join(keep) + "\n", encoding="utf-8")
        _owned(EVENTS)


CHECKS = [("docker", _check_docker), ("sandbox", _check_sandbox_infra), ("orphans", _check_orphans),
          ("disk", _check_disk), ("web", _check_web), ("housekeeping", _housekeeping)]


def watchdog() -> list[str]:
    """Each check is isolated: one failing check (or a bug in it) never stops the others. The healer only has a
    fixed list of safe, idempotent actions (restart a service, recreate the proxy/network, remove orphans, prune
    unused images, roll back to the last good commit). No LLM decides anything here."""
    if os.getenv("PAPERCI_WATCHDOG_SIMULATE_CRASH") == "1":  # healer drill: prove the shell fallback takes over
        raise RuntimeError("simulated watchdog crash (drill)")
    st = _load(WATCH_STATE)
    acts: list[str] = []
    for name, fn in CHECKS:
        try:
            fn(st, acts)
        except Exception as ex:
            errs = st.setdefault("check_errors", {})
            if time.time() - errs.get(name, 0) > 3600:  # report a broken check at most hourly
                event("error", f"Watchdog check '{name}' failed: {type(ex).__name__}: {str(ex)[:160]}")
                errs[name] = time.time()
    st["last_run"] = time.time()
    _save(WATCH_STATE, st)
    return acts


# ---------------------------------------------------------------- digest
def digest() -> Path:
    from . import db, ledger
    from .recovery import load_playbook
    since = time.time() - 86400
    ev = [e for e in events(2000) if e["ts"] >= since]
    by = lambda k: [e for e in ev if e["kind"] == k]  # noqa: E731
    builds = db.q("SELECT output FROM stages WHERE stage='build' AND status='done' AND finished_at>=?", (since,))
    built = sum(1 for b in builds if json.loads(b["output"]).get("status") == "built")
    pb = load_playbook()
    day = time.strftime("%Y-%m-%d", _local())
    L = [f"# PaperCI daily digest, {day}", "",
         f"- Reproduction attempts: {len(builds)} ({built} built and verified offline)",
         f"- Reviews written: {len(by('review'))}",
         f"- Autopilot cycles: {sum(1 for e in by('cycle') if 'done' in e['text'])}",
         f"- Self-improvement: playbook generation {pb.generation}, {len(pb.rules)} rules",
         f"- Incidents auto-repaired: {sum(1 for e in by('incident') if e.get('healed'))} of {len(by('incident'))}",
         f"- Chaos drills passed: {sum(1 for e in by('chaos') if e.get('recovered'))} of {len(by('chaos'))}",
         f"- LLM spend today: ${ledger.today_total():.2f}", "", "## Timeline", ""]
    L += [f"- {time.strftime('%H:%M', _local(e['ts']))} [{e['kind']}] {e['text']}" for e in ev[-60:]]
    p = OPS / f"digest-{day}.md"
    p.write_text("\n".join(L) + "\n", encoding="utf-8")
    _owned(p)
    event("digest", f"Daily digest written: {len(builds)} attempts, {built} built, {len(by('review'))} reviews",
          file=p.name)
    return p


def main(argv: list[str] | None = None) -> int:
    a = (argv or sys.argv[1:]) or ["events"]
    if a[0] == "watchdog":
        print(json.dumps(watchdog()))
    elif a[0] == "nightly":
        print(json.dumps(nightly(), default=str, indent=1))
    elif a[0] == "chaos":
        print(json.dumps(chaos_drill(a[1] if len(a) > 1 else None), default=str))
    elif a[0] == "digest":
        print(digest())
    elif a[0] == "cycle":
        print(json.dumps(cycle()))
    elif a[0] == "event" and len(a) >= 3:  # used by deploy/*.sh: event <kind> <text> [key=value ...]
        extra = dict(x.split("=", 1) for x in a[3:] if "=" in x)
        for k in ("healed", "recovered", "passed"):
            if k in extra:
                extra[k] = extra[k] == "true"
        event(a[1], a[2], **extra)
    elif a[0] == "health":
        ok, info = health()
        print(json.dumps({"ok": ok, **info}))
        return 0 if ok else 1
    else:
        for e in events(40):
            print(time.strftime("%m-%d %H:%M", _local(e["ts"])), e["kind"], e["text"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
