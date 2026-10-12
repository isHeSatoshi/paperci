"""Referee guard: Entire checkpoints + entire-graph change analysis for PaperCI's own commits.

The self-healing loop is only trustworthy if the referee stays fixed: a commit that touches a file in
evolve.FROZEN (verifier, sealed claims, ledger, sandbox allowlist...) changes how candidates are scored, so it
must never land in the middle of an evolution round. This module asks entire-graph which code entities each
commit really changed, labels the risk, and links every commit to the Entire checkpoint (the agent session)
that produced it.

  python -m paperci.entire_guard gate [--base origin/main] [--head HEAD]   # exit 2 if the referee is touched
  python -m paperci.entire_guard report                                    # docs/entire-graph.{json,md}

Needs the Entire CLI with the graph plugin (`entire plugin install graph`). No network: entire-graph is local.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GH = "https://github.com/isHeSatoshi/paperci"
ENTIRE = "https://entire.io/gh/isHeSatoshi/paperci"
WIDE_DEPENDENTS = 3  # a changed signature with this many callers deserves the full test suite first


def frozen() -> list[str]:
    from .evolve import FROZEN
    return list(FROZEN)


def _git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=60).stdout


def graph_diff(base: str, head: str) -> dict:
    r = subprocess.run(["entire", "graph", "diff", "--base", base, "--head", head, "--json", "--repo", str(ROOT)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return {"files": [], "error": (r.stderr or r.stdout).strip()[:300]}


def changed_paths(base: str, head: str) -> list[str]:
    return [p for p in _git("diff", "--name-only", base, head).splitlines() if p]


def assess(diff: dict, frozen_paths: list[str], paths: list[str] | None = None) -> dict:
    """Risk label for one diff. `referee` beats `wide` beats `contained`.

    Referee files are matched on git's changed-path list too: the allowlist is not a parsed language, so the graph
    alone would miss it."""
    wide, entities = [], 0
    every = set(paths or []) | {f.get("path", "") for f in (diff.get("files") or [])}
    referee = sorted(p for p in every if any(p == z or p.startswith(z.rstrip("/") + "/") for z in frozen_paths))
    for f in (diff.get("files") or []):
        path = f.get("path", "")
        changes = f.get("changes") or []
        entities += len(changes)
        if f.get("language") in (None, "", "Markdown"):
            continue
        for c in changes:
            if c.get("type") in ("signature_changed", "removed", "renamed") and (c.get("dependents_count") or 0) >= WIDE_DEPENDENTS:
                wide.append(f"{path}:{c.get('name')} ({c.get('type')}, {c.get('dependents_count')} dependents)")
    if referee:
        level, why = "referee", "touches the frozen referee; deploy only between evolution rounds"
    elif wide:
        level, why = "wide", "changes an interface many callers use; run the full offline suite first"
    else:
        level, why = "contained", "no referee files, no widely used interface changed"
    return {"level": level, "why": why, "referee_files": referee, "wide_changes": wide[:8],
            "files": len((diff.get("files") or [])), "entities": entities}


def top_entities(diff: dict, n: int = 6) -> list[str]:
    out = []
    for f in (diff.get("files") or []):
        for c in f.get("changes") or []:
            out.append((c.get("dependents_count") or 0, f"{c.get('type')} {c.get('kind')} {f.get('path')}:{c.get('name')}"))
    return [s for _, s in sorted(out, key=lambda x: -x[0])[:n]]


def checkpointed_commits() -> list[dict]:
    out = _git("log", "--first-parent", "--format=%H%x1f%P%x1f%ct%x1f%s%x1f%(trailers:key=Entire-Checkpoint,valueonly,separator=%x2C)%x1e")
    rows = []
    for rec in out.split("\x1e"):
        p = rec.strip("\n").split("\x1f")
        if len(p) >= 5 and p[4].strip():
            rows.append({"sha": p[0], "parent": (p[1].split() or [""])[0], "ts": int(p[2]), "subject": p[3],
                         "checkpoint": p[4].strip().split(",")[0]})
    return rows


def report(out_dir: Path) -> dict:
    fz = frozen()
    commits = []
    for c in checkpointed_commits():
        if not c["parent"]:
            continue
        d = graph_diff(c["parent"], c["sha"])
        a = assess(d, fz, changed_paths(c["parent"], c["sha"]))
        commits.append({**c, **a, "top": top_entities(d), "graph_error": d.get("error"),
                        "entire_url": f"{ENTIRE}/commit/{c['sha']}", "github_url": f"{GH}/commit/{c['sha']}"})
    counts = {k: sum(1 for c in commits if c["level"] == k) for k in ("referee", "wide", "contained")}
    data = {"generated": int(time.time()), "frozen": fz, "commits": commits, "counts": counts,
            "tool": "entire graph diff (entire-graph plugin) per checkpointed commit"}
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "entire-graph.json").write_text(json.dumps(data, indent=1), encoding="utf-8")
    (out_dir / "entire-graph.md").write_text(_markdown(data), encoding="utf-8")
    return data


def _markdown(data: dict) -> str:
    c = data["counts"]
    lines = [
        "# Entire checkpoints x entire-graph: what each agent session changed",
        "",
        "Generated by `python -m paperci.entire_guard report`. For every commit that carries an Entire checkpoint,",
        "`entire graph diff` lists the code entities that changed and how many callers depend on them. The guard labels",
        "the risk for PaperCI's self-healing loop:",
        "",
        "- **referee**: touches a file the evolution loop scores against (`evolve.FROZEN`). Shipping it mid-round would",
        "  change the rules while candidates are being judged, so it is deployed only between rounds.",
        f"- **wide**: changes the signature of something with {WIDE_DEPENDENTS}+ dependents. Run the full offline suite first.",
        "- **contained**: everything else.",
        "",
        f"**{len(data['commits'])} checkpointed commits: {c['referee']} referee, {c['wide']} wide, {c['contained']} contained.**",
        "",
        "| Date (UTC) | Commit | What the session did | Risk | Entities | Checkpoint |",
        "|---|---|---|---|---|---|",
    ]
    for r in data["commits"]:
        when = time.strftime("%m-%d %H:%M", time.gmtime(r["ts"]))
        subj = r["subject"].replace("|", "/")[:110]
        lines.append(f"| {when} | [`{r['sha'][:8]}`]({r['github_url']}) | {subj} | **{r['level']}** | {r['entities']} "
                     f"| [{r['checkpoint']}]({r['entire_url']}) |")
    risky = [r for r in data["commits"] if r["level"] != "contained"]
    if risky:
        lines += ["", "## Why the risky ones were flagged", ""]
        for r in risky:
            lines.append(f"- [`{r['sha'][:8]}`]({r['entire_url']}) {r['subject'][:90]}: {r['why']}.")
            for f in r["referee_files"]:
                lines.append(f"  - referee file: `{f}`")
            for w in r["wide_changes"][:3]:
                lines.append(f"  - wide change: `{w}`")
    lines += ["", "Frozen referee files: " + ", ".join(f"`{p}`" for p in data["frozen"]), ""]
    return "\n".join(lines)


def gate(base: str, head: str) -> int:
    a = assess(graph_diff(base, head), frozen(), changed_paths(base, head))
    print(f"{base}..{head}: {a['level']} ({a['files']} files, {a['entities']} entities) - {a['why']}")
    for f in a["referee_files"]:
        print(f"  referee file: {f}")
    for w in a["wide_changes"]:
        print(f"  wide change: {w}")
    return 2 if a["level"] == "referee" else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="paperci.entire_guard")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gate"); g.add_argument("--base", default="origin/main"); g.add_argument("--head", default="HEAD")
    r = sub.add_parser("report"); r.add_argument("--out", default=str(ROOT / "docs"))
    a = ap.parse_args(argv)
    if a.cmd == "gate":
        return gate(a.base, a.head)
    d = report(Path(a.out))
    print(f"{len(d['commits'])} checkpointed commits analysed: {d['counts']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
