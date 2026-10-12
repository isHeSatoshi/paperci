"""neatlogs public API: PaperCI's failure clusters become native neatlogs incidents.

The SDK (tracing.py) sends spans; this module closes the loop the other way. Every unrecovered builder failure is
already a row in `failures` with the trace it happened in, so each failure category in the last 24 h is one
incident snapshot (fingerprint = category) whose occurrences link straight to those traces, with what PaperCI's
playbook already knows about the category. A category with no unrecovered failures left gets an empty snapshot,
so a fix shows up as the incident going quiet.

Needs a service-account token (incident publishing accepts nothing else): NEATLOGS_TOKEN, NEATLOGS_PROJECT_ID,
NEATLOGS_ORG_ID. Without them every call is a no-op. Best effort throughout: telemetry never fails a job.

    python -m paperci.nl_api incidents [--dry-run]
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

from .config import ROOT

HOST = os.getenv("NEATLOGS_HOST", "https://app.neatlogs.com").rstrip("/")
STATE = ROOT / "data" / "neatlogs_incidents.json"
WINDOW_S = 24 * 3600
MIN_GAP_S = int(os.getenv("PAPERCI_NL_INCIDENT_EVERY", "1800"))
WORKFLOW = "paperci-reproduce-paper"


def enabled() -> bool:
    return all(os.getenv(k) for k in ("NEATLOGS_TOKEN", "NEATLOGS_PROJECT_ID", "NEATLOGS_ORG_ID"))


def _post(path: str, body: dict, idem: str) -> dict:
    req = urllib.request.Request(
        f"{HOST}/api/v1/public{path}", data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {os.environ['NEATLOGS_TOKEN']}", "x-project-id": os.environ["NEATLOGS_PROJECT_ID"],
                 "Content-Type": "application/json", "Idempotency-Key": idem, "User-Agent": "paperci"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        # The problem document never echoes the credential; keep it short anyway.
        raise RuntimeError(f"HTTP {e.code}: {e.read()[:1500].decode(errors='replace')}") from None


def _get(path: str) -> dict:
    req = urllib.request.Request(
        f"{HOST}/api/v1/public{path}",
        headers={"Authorization": f"Bearer {os.environ['NEATLOGS_TOKEN']}", "x-project-id": os.environ["NEATLOGS_PROJECT_ID"],
                 "User-Agent": "paperci"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read() or b"{}")


def _trace_starts(trace_ids: set[str], cache: dict) -> dict:
    """Occurrences must carry the trace's canonical start time as stored by neatlogs (anything else is rejected
    as invalid evidence); traces neatlogs never received are left out."""
    for tid in trace_ids - cache.keys():
        try:
            cache[tid] = (_get(f"/traces/{tid}").get("data") or {}).get("startedAt")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                cache[tid] = None
        except Exception:
            pass
    return cache


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _load() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _playbook_rules(category: str) -> list[dict]:
    try:
        pb = json.loads((ROOT / "data" / "playbook.json").read_text(encoding="utf-8"))
        return [r for r in pb.get("rules", []) if r.get("category") == category]
    except Exception:
        return []


def snapshots(now: float | None = None) -> list[dict]:
    """One incident snapshot per failure category with unrecovered failures in the window."""
    from . import db
    now = now or time.time()
    rows = db.q("SELECT id, ts, arxiv_id, run_label, stage, category, detail, trace_id FROM failures "
                "WHERE ts > ? AND recovered = 0 AND trace_id IS NOT NULL AND trace_id != '' ORDER BY ts", (now - WINDOW_S,))
    by: dict[str, list] = {}
    for r in rows:
        by.setdefault(r["category"], []).append(dict(r))
    out = []
    for cat, fs in by.items():
        papers = sorted({f["arxiv_id"] for f in fs})
        rules = _playbook_rules(cat)
        examples = [f"{f['arxiv_id']} [{f['run_label']}] {f['stage']}: {f['detail'][:300]}" for f in fs[-5:]]
        playbook = (f"PaperCI's playbook already has {len(rules)} rule(s) for this category "
                    f"({', '.join(r['rule_id'] for r in rules[:4])}); failures still happen, so the next evolution "
                    f"round can propose a better rule and test it on the benchmark." if rules else
                    "No playbook rule covers this category yet; the evolution loop proposes one only from dev-split "
                    "failures and adopts it only if the benchmark improves.")
        out.append({
            "category": cat, "papers": papers, "n": len(fs),
            "title": f"Builder failure: {cat} ({len(fs)} in {len(papers)} paper(s), last 24 h)"[:240],
            "severity": "high" if len(fs) >= 10 or len(papers) >= 5 else "medium" if len(fs) >= 3 else "low",
            "description": (f"PaperCI's builder agent hit '{cat}' {len(fs)} time(s) without recovering, in "
                            f"{len(papers)} paper(s): {', '.join(papers[:12])}. Each occurrence links to the run's "
                            f"trace.\n\n{playbook}\n\nLatest examples:\n" + "\n".join(f"- {s}" for s in examples))[:20000],
            "occurrences": [{"source_event_id": f"failure-{f['id']}", "trace_id": f["trace_id"],
                             "observed_at": _iso(f["ts"])} for f in fs[-100:]],
        })
    return out


def publish_incidents(force: bool = False, dry_run: bool = False) -> dict:
    if not enabled() and not dry_run:
        return {"skipped": "no neatlogs service-account token"}
    st = _load()
    now = time.time()
    if not force and not dry_run and now - st.get("last", 0) < MIN_GAP_S:
        return {"skipped": "published recently"}
    snaps = snapshots(now)
    if not dry_run:
        wanted = {o["trace_id"] for s in snaps for o in s["occurrences"]}
        starts = _trace_starts(wanted, {k: v for k, v in st.get("trace_start", {}).items() if k in wanted})
        st["trace_start"] = starts
        for s in snaps:
            s["occurrences"] = [{**o, "observed_at": starts[o["trace_id"]]} for o in s["occurrences"]
                                if starts.get(o["trace_id"])]
        snaps = [s for s in snaps if s["occurrences"]]
    live = {s["category"] for s in snaps}
    quiet = [c for c in st.get("open", {}) if c not in live]
    res: dict = {"published": [], "resolved": [], "errors": []}
    for s in snaps + [{"category": c, "quiet": True} for c in quiet]:
        cat = s["category"]
        rev = int(now)  # monotonic per fingerprint without extra bookkeeping
        quiet_one = s.get("quiet")
        body = {
            "project_id": os.getenv("NEATLOGS_PROJECT_ID", ""), "org_id": os.getenv("NEATLOGS_ORG_ID", ""),
            "source": "incident_loop", "fingerprint": f"paperci:builder:{cat}"[:255], "revision": rev,
            "complete_snapshot": True, "window_start": _iso(now - WINDOW_S), "window_end": _iso(now),
            "workflow": WORKFLOW, "category": cat[:32],
            "title": (f"Builder failure: {cat} (no unrecovered occurrences in the last 24 h)" if quiet_one
                      else s["title"])[:240],
            "description": ("No unrecovered occurrences of this failure category in the last 24 h." if quiet_one
                            else s["description"]),
            "severity": "low" if quiet_one else s["severity"],
            "failure_occurrences": [] if quiet_one else s["occurrences"],
            # Both of these may cite only this snapshot's own occurrences; the API rejects free-text samples and
            # externally written findings as invalid evidence, so the analysis lives in the description.
            "representative_samples": [] if quiet_one else [o["source_event_id"] for o in s["occurrences"][-5:]],
            "presentation": {
                "version": 1, "findings": [],
                "open_questions": [] if quiet_one else [
                    "Is this an environment limit (allowlist, CPU, data access) or an agent behaviour the playbook "
                    "can fix?"],
                "coverage": {"judged_targets": 0 if quiet_one else len(s["papers"]), "census_turns": None,
                             "complete": None,
                             "limitations": ["Counts only builder failures PaperCI classified; recovered failures "
                                             "are excluded."]},
                "monitoring": [],
            },
        }
        if dry_run:
            res["published"].append({"fingerprint": body["fingerprint"], "n": len(body["failure_occurrences"])})
            continue
        try:
            # The API takes only UUIDv4 keys; derive one so a retry of the same snapshot is deduplicated.
            key = uuid.UUID(bytes=hashlib.sha256(f"{body['fingerprint']}#{rev}".encode()).digest()[:16], version=4)
            _post("/incidents/external", body, str(key))
            (res["resolved"] if quiet_one else res["published"]).append(cat)
        except Exception as e:
            res["errors"].append(f"{cat}: {str(e)[:600]}")
    if not dry_run:
        st["last"] = now
        # Still open: published now, or quiet but the resolving snapshot failed (retried next time).
        st["open"] = {c: int(now) for c in res["published"]} | {
            c: st["open"][c] for c in quiet if c not in res["resolved"]}
        try:
            STATE.parent.mkdir(parents=True, exist_ok=True)
            STATE.write_text(json.dumps(st, indent=1), encoding="utf-8")
        except Exception:
            pass
    return res


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if argv[:1] == ["incidents"]:
        print(json.dumps(publish_incidents(force=True, dry_run="--dry-run" in argv), indent=1))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
