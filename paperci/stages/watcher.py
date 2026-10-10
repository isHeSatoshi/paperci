"""Watcher: poll arXiv for fresh papers that link public code."""
from __future__ import annotations

import hashlib

from .. import db, sources, tracing

DEFAULT_QUERIES = [
    "abs:github AND (cat:stat.ML OR cat:math.OC OR cat:stat.CO OR cat:cs.NE)",
    "abs:github AND cat:cs.LG",
]


def split_for(arxiv_id: str) -> str:
    # Stable assignment: ~1/3 of papers are held out and never used to tune the builder.
    h = int(hashlib.sha256(arxiv_id.encode()).hexdigest(), 16)
    return "heldout" if h % 3 == 0 else "dev"


def watch(queries: list[str] | None = None, per_query: int = 60, since: str | None = None) -> dict:
    found = new = 0
    with tracing.span("stage:watcher", kind="TOOL", queries=queries or DEFAULT_QUERIES):
        for q in queries or DEFAULT_QUERIES:
            for p in sources.arxiv_search(q, max_results=per_query):
                if since and p["published"][:10] < since:
                    continue
                if not p["repo_url"]:
                    continue
                found += 1
                p["split"] = split_for(p["arxiv_id"])
                if db.upsert_paper(p):
                    new += 1
        tracing.log("watcher found {found} code papers, {new} new", found=found, new=new)
    return {"found_with_code": found, "new": new}
