"""Environment learning: packages that builders keep installing across different
papers are promoted into the sandbox base image, so future builds start closer
to working. Gate: the package must install cleanly in a fresh sandbox."""
from __future__ import annotations

from . import db, tracing
from .config import ROOT
from .sandbox import Sandbox

BASE = ROOT / "sandbox" / "runner" / "base-packages.txt"
NEVER = {"pip", "setuptools", "wheel", "torch", "torchvision", "torchaudio", "nvidia", "cuda", "tensorflow-gpu"}


def propose(min_papers: int = 2, apply: bool = False) -> dict:
    base = {l.strip().lower() for l in BASE.read_text().splitlines() if l.strip()}
    rows = db.q("SELECT package, COUNT(DISTINCT arxiv_id) n FROM package_installs GROUP BY package HAVING n>=? ORDER BY n DESC",
                (min_papers,))
    cands = [r["package"] for r in rows if r["package"] not in base and r["package"] not in NEVER
             and r["package"][:1].isalpha()
             and not r["package"].startswith(("nvidia-", "cuda"))][:15]
    if not cands:
        return {"candidates": [], "promoted": []}
    with tracing.span("stage:envlearn", kind="TOOL", candidates=cands) as s:
        sb = Sandbox.start("envlearn", network="proxy")
        ok = []
        try:
            for p in cands:
                r = sb.exec(f"pip install -q --no-cache-dir {p} && python -c 'import importlib.metadata as m; print(m.version(\"{p}\"))'", timeout=300)
                if r.exit_code == 0:
                    ok.append(p)
        finally:
            sb.remove()
        tracing.set_attr(s, "promoted", ok)
    if apply and ok:
        BASE.write_text("\n".join(sorted(base | set(ok))) + "\n", newline="\n")
    return {"candidates": cands, "promoted": ok, "applied": apply,
            "note": "rebuild with scripts/build_images.ps1 -Tag gen1 and set PAPERCI_SANDBOX_IMAGE" if apply and ok else ""}
