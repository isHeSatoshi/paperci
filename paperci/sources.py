"""arXiv + GitHub readers (host side, read-only HTTP; nothing here executes repo code)."""
from __future__ import annotations

import html
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from functools import lru_cache
from typing import Any

import requests

UA = {"User-Agent": "PaperCI/0.1 (hackathon research prototype; contact via GitHub repo)"}
ATOM = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
GH_RE = re.compile(r"https?://(?:www\.)?github\.com/([A-Za-z0-9_.\-]+)/([A-Za-z0-9_.\-]+)")
_last_arxiv = [0.0]


def _arxiv_get(url: str, **kw: Any) -> requests.Response:
    # arXiv asks clients to wait 3s between requests.
    wait = 3.1 - (time.time() - _last_arxiv[0])
    if wait > 0:
        time.sleep(wait)
    for attempt in range(3):
        r = requests.get(url, headers=UA, timeout=60, **kw)
        _last_arxiv[0] = time.time()
        if r.status_code in (429, 503):
            time.sleep(10 * (attempt + 1))
            continue
        return r
    return r


def find_github(text: str) -> str | None:
    for owner, repo in GH_RE.findall(text or ""):
        repo = re.sub(r"\.git$|[.,;:)\]]+$", "", repo)
        if owner.lower() in ("features", "orgs", "topics", "sponsors", "settings") or not repo:
            continue
        return f"https://github.com/{owner}/{repo}"
    return None


def arxiv_search(query: str, max_results: int = 50, start: int = 0) -> list[dict]:
    r = _arxiv_get("http://export.arxiv.org/api/query", params={
        "search_query": query, "start": start, "max_results": max_results,
        "sortBy": "submittedDate", "sortOrder": "descending"})
    r.raise_for_status()
    root = ET.fromstring(r.text)
    out = []
    for e in root.findall("a:entry", ATOM):
        aid = e.findtext("a:id", "", ATOM).rsplit("/abs/", 1)[-1]
        aid = re.sub(r"v\d+$", "", aid)
        abstract = " ".join((e.findtext("a:summary", "", ATOM) or "").split())
        comment = e.findtext("arxiv:comment", "", ATOM) or ""
        cats = [c.get("term") for c in e.findall("a:category", ATOM)]
        out.append({
            "arxiv_id": aid,
            "title": " ".join((e.findtext("a:title", "", ATOM) or "").split()),
            "abstract": abstract, "comment": comment, "categories": cats,
            "published": e.findtext("a:published", "", ATOM),
            "repo_url": find_github(abstract + " " + comment),
        })
    return out


def _strip_html(s: str) -> str:
    s = re.sub(r"(?is)<(script|style|math|svg|nav|header|footer)[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"(?i)<annotation[^>]*>(.*?)</annotation>", r" \1 ", s)
    s = re.sub(r"(?i)</(p|div|tr|h\d|li|table|caption|section)>", "\n", s)
    s = re.sub(r"(?i)<(td|th)[^>]*>", " | ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\u00a0]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n", s).strip()


def arxiv_fulltext(arxiv_id: str) -> tuple[str, str]:
    """Return (text, source). Uses the arXiv HTML rendering when available."""
    r = _arxiv_get(f"https://arxiv.org/html/{arxiv_id}")
    if r.status_code == 200 and len(r.text) > 5000:
        return _strip_html(r.text), "arxiv-html"
    r = _arxiv_get(f"https://arxiv.org/abs/{arxiv_id}")
    m = re.search(r'(?s)<blockquote class="abstract[^"]*">(.*?)</blockquote>', r.text)
    return (_strip_html(m.group(1)) if m else ""), "arxiv-abstract"


def paper_links(arxiv_id: str) -> str | None:
    """Look for a GitHub link in the paper HTML when the abstract has none."""
    r = _arxiv_get(f"https://arxiv.org/html/{arxiv_id}")
    if r.status_code != 200:
        return None
    return find_github(r.text)


@lru_cache(maxsize=1)
def _gh_token() -> str | None:
    # Read-only use of the operator's existing GitHub CLI login to avoid the
    # 60 req/h anonymous limit. Kept in memory only; never sent to sandboxes.
    try:
        p = subprocess.run(["gh", "auth", "token"], capture_output=True, timeout=15, text=True)
        tok = p.stdout.strip()
        return tok or None
    except Exception:
        return None


def gh_api(path: str) -> requests.Response:
    h = dict(UA, Accept="application/vnd.github+json")
    tok = _gh_token()
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return requests.get(f"https://api.github.com/{path.lstrip('/')}", headers=h, timeout=30)


def repo_snapshot(repo_url: str) -> dict:
    """Metadata, pinned commit, file tree and README for a GitHub repo."""
    m = GH_RE.match(repo_url)
    if not m:
        return {"error": "not a github url"}
    owner, name = m.group(1), re.sub(r"\.git$", "", m.group(2))
    r = gh_api(f"repos/{owner}/{name}")
    if r.status_code != 200:
        return {"error": f"repo api {r.status_code}", "owner": owner, "name": name}
    meta = r.json()
    branch = meta.get("default_branch", "main")
    c = gh_api(f"repos/{owner}/{name}/commits/{branch}")
    sha = c.json().get("sha") if c.status_code == 200 else None
    tree: list[dict] = []
    if sha:
        t = gh_api(f"repos/{owner}/{name}/git/trees/{sha}?recursive=1")
        if t.status_code == 200:
            tree = [{"path": x["path"], "size": x.get("size", 0)} for x in t.json().get("tree", [])
                    if x.get("type") == "blob"]
    readme = ""
    rr = gh_api(f"repos/{owner}/{name}/readme")
    if rr.status_code == 200:
        dl = rr.json().get("download_url")
        if dl:
            readme = requests.get(dl, headers=UA, timeout=30).text
    return {
        "owner": owner, "name": name, "full_name": meta.get("full_name"), "sha": sha, "branch": branch,
        "size_kb": meta.get("size", 0), "language": meta.get("language"), "stars": meta.get("stargazers_count"),
        "archived": meta.get("archived"), "license": (meta.get("license") or {}).get("spdx_id"),
        "pushed_at": meta.get("pushed_at"), "tree": tree, "readme": readme,
    }
