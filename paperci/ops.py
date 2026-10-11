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
    st = _load(STATE)
    if kind == "nightly":
        st["nightly_day"] = time.strftime("%Y-%m-%d", _local())
        _save(STATE, st)
        _sub([sys.executable, "-u", "-m", "paperci.ops", "nightly"], "nightly")
    else:
        st["last_cycle"] = time.time()
        _save(STATE, st)
        cycle()


def _sub(cmd: list[str], name: str, timeout: int = 3 * 3600) -> tuple[int, str]:
    log = OPS / f"{name}-{time.strftime('%Y%m%d-%H%M', _local())}.log"
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    try:
        p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, env=env)
        out, rc = p.stdout + p.stderr, p.returncode
    except subprocess.TimeoutExpired as ex:
        out, rc = (ex.stdout or "") if isinstance(ex.stdout, str) else "", 124
    log.write_text(out, encoding="utf-8")
    return rc, out


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
def _web_ok() -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{WEB_PORT}/healthz", timeout=10) as r:
            return r.status == 200
    except Exception:
        return False


def _sh(*cmd: str, timeout: int = 300) -> tuple[int, str]:
    try:
        p = subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr)[-500:]
    except Exception as ex:
        return 1, str(ex)


def watchdog() -> list[str]:
    from .sandbox import docker_ok, ensure_infra
    st = _load(WATCH_STATE)
    acts: list[str] = []
    t0 = time.time()
    if not docker_ok():
        _sh("systemctl", "restart", "docker", timeout=180)
        ok = docker_ok()
        event("incident", f"Docker daemon was not responding; restarted it ({'healthy' if ok else 'STILL DOWN'})",
              healed=ok, component="docker", seconds=round(time.time() - t0, 1))
        acts.append("restarted docker")
    if docker_ok():
        try:
            for a in ensure_infra():
                if a.startswith("WARNING"):
                    continue
                if not st.get("last_run"):  # first start on a fresh host is setup, not an incident
                    event("boot", f"First start: sandbox infrastructure set up ({a})")
                else:
                    event("incident", f"Sandbox infrastructure drift repaired: {a}", healed=True, component="sandbox")
                acts.append(a)
        except Exception as ex:
            event("incident", f"Could not repair sandbox infrastructure: {str(ex)[:160]}", healed=False, component="sandbox")
    free = shutil.disk_usage("/").free / 1e9
    if free < DISK_MIN_GB:
        _sh("docker", "builder", "prune", "-af", timeout=600)
        _sh("docker", "image", "prune", "-f", timeout=600)
        rc, imgs = _sh("docker", "images", "--format", "{{.Repository}}:{{.Tag}}", "--filter", "reference=paperci/built*")
        old = [i for i in imgs.split() if i.startswith("paperci/built")]
        if old:
            _sh("docker", "rmi", "-f", *old, timeout=600)
        after = shutil.disk_usage("/").free / 1e9
        event("incident", f"Disk pressure ({free:.1f} GB free); pruned build cache and {len(old)} old build image(s): "
                          f"{after:.1f} GB free now", healed=after >= DISK_MIN_GB, component="disk", freed_gb=round(after - free, 1))
        acts.append("pruned disk")
    if _web_ok():
        st["web_fail"] = 0
    else:
        st["web_fail"] = st.get("web_fail", 0) + 1
        if st["web_fail"] >= 2:
            _sh("systemctl", "restart", "paperci-web", timeout=120)
            time.sleep(8)
            ok = _web_ok()
            event("incident", f"Web app failed two health checks; restarted it ({'healthy' if ok else 'STILL DOWN'})",
                  healed=ok, component="web")
            acts.append("restarted web")
            st["web_fail"] = 0
    st["last_run"] = time.time()
    st["disk_free_gb"] = round(free, 1)
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
    else:
        for e in events(40):
            print(time.strftime("%m-%d %H:%M", _local(e["ts"])), e["kind"], e["text"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
