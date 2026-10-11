"""arXiv + GitHub readers (host side, read-only HTTP; nothing here executes repo code)."""
from __future__ import annotations

import html
import os
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


UPLOAD_DIR = __import__("pathlib").Path(__file__).resolve().parent.parent / "data" / "uploads"


def upload_id(pdf_bytes: bytes) -> str:
    import hashlib
    return "u-" + hashlib.sha256(pdf_bytes).hexdigest()[:10]


PAPER_FORMATS = ".pdf .tex .zip .tar .tar.gz .tgz .docx .md .txt .html .htm"


def _latex_to_text(tex: str) -> tuple[str, str]:
    """Readable text from LaTeX source (enough for claim extraction; numbers and prose survive)."""
    m = re.search(r"\\title\s*(?:\[[^\]]*\])?\s*\{((?:[^{}]|\{[^{}]*\})*)\}", tex)
    title = re.sub(r"\\[a-zA-Z]+\*?|[{}]", " ", m.group(1)) if m else ""
    t = re.sub(r"(?<!\\)%.*", "", tex)
    t = re.sub(r"\\begin\{(figure|tikzpicture|algorithm)\*?\}.*?\\end\{\1\*?\}", " ", t, flags=re.S)
    t = re.sub(r"\\(cite|ref|label|eqref|citep|citet|url|includegraphics)\*?(\[[^\]]*\])?\{[^}]*\}", " ", t)
    t = re.sub(r"\\(section|subsection|subsubsection|paragraph|caption|textbf|textit|emph|mathrm|text)\*?\{", "\n", t)
    t = re.sub(r"&", " | ", t)
    t = re.sub(r"\\\\", "\n", t)
    t = re.sub(r"\\[a-zA-Z@]+\*?", " ", t)
    t = re.sub(r"[{}$~^_]", " ", t)
    t = re.sub(r"\\%", "%", t)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", t)).strip(), " ".join(title.split())


def _latex_bundle(files: dict[str, str]) -> str:
    """arXiv-style source bundle: start at the file with \\documentclass and inline \\input/\\include."""
    main = next((n for n, s in files.items() if "\\documentclass" in s), None) or max(files, key=lambda n: len(files[n]))
    seen = set()

    def expand(name: str, depth: int = 0) -> str:
        if depth > 6 or name in seen:
            return ""
        seen.add(name)
        src = files.get(name) or files.get(name + ".tex") or ""
        return re.sub(r"\\(?:input|include)\{([^}]+)\}",
                      lambda m: expand(m.group(1).strip().removeprefix("./"), depth + 1), src)
    return expand(main)


def paper_text(data: bytes, filename: str) -> tuple[str, str, str]:
    """(text, title_hint, format) for an uploaded paper in any common academic format. Parsing only;
    nothing inside the upload is executed."""
    import io
    import tarfile
    import zipfile
    name = (filename or "").lower()
    if data[:4] == b"%PDF" or name.endswith(".pdf"):
        from pypdf import PdfReader
        r = PdfReader(io.BytesIO(data))
        t = (r.metadata.title if r.metadata and r.metadata.title else "") or ""
        return "\n".join((p.extract_text() or "") for p in r.pages[:60]), t, "pdf"
    if name.endswith(".docx"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
        xml = re.sub(r"</w:p>", "\n", xml)
        return html.unescape(re.sub(r"<[^>]+>", "", xml)), "", "docx"
    if name.endswith(".zip") or data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            files = {i.filename.removeprefix("./"): z.read(i).decode("utf-8", "replace") for i in z.infolist()
                     if i.filename.endswith(".tex") and i.file_size < 3_000_000}
        if not files:
            raise ValueError("the archive contains no .tex files")
        text, title = _latex_to_text(_latex_bundle(files))
        return text, title, "latex-zip"
    if name.endswith((".tar", ".tar.gz", ".tgz")) or data[:2] == b"\x1f\x8b":
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
            files = {}
            for m in tf.getmembers():
                if m.isfile() and m.name.endswith(".tex") and m.size < 3_000_000:
                    f = tf.extractfile(m)  # read into memory only; nothing is written to disk
                    files[m.name.removeprefix("./")] = f.read().decode("utf-8", "replace") if f else ""
        if not files:
            raise ValueError("the archive contains no .tex files")
        text, title = _latex_to_text(_latex_bundle(files))
        return text, title, "latex-tar"
    s = data.decode("utf-8", "replace")
    if name.endswith(".tex") or "\\documentclass" in s or "\\begin{document}" in s:
        text, title = _latex_to_text(s)
        return text, title, "latex"
    if name.endswith((".html", ".htm")) or s.lstrip().lower().startswith(("<!doctype", "<html")):
        m = re.search(r"(?is)<title>(.*?)</title>", s)
        return _strip_html(s), (html.unescape(m.group(1)).strip() if m else ""), "html"
    m = re.search(r"(?m)^#\s+(.+)$", s)
    return s, (m.group(1).strip() if m else ""), "markdown" if name.endswith(".md") else "text"


def ingest_pdf(pdf_bytes: bytes, repo_url: str | None = None, title: str | None = None,
               filename: str = "paper.pdf") -> dict:
    """An unpublished paper uploaded by its authors (any format in PAPER_FORMATS): text is extracted on the
    host and kept locally; claims are extracted from it exactly as for arXiv papers."""
    text, meta_title, fmt = paper_text(pdf_bytes, filename)
    if len(text) < 2000:
        raise ValueError(f"could not extract enough text from this {fmt} file")
    pid = upload_id(pdf_bytes)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    (UPLOAD_DIR / f"{pid}.txt").write_text(text, encoding="utf-8")
    first = next((l.strip() for l in text.splitlines() if len(l.strip()) > 12), "Untitled paper")
    m = re.search(r"(?is)\babstract\b[\s.:—-]*(.{200,1800}?)(?:\n\s*\n|\b1\.?\s+Introduction\b|\bIntroduction\b)", text)
    return {"arxiv_id": pid, "title": (title or meta_title or first)[:300],
            "abstract": " ".join((m.group(1) if m else text[:1500]).split()), "comment": "",
            "categories": ["upload"], "published": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "repo_url": find_github(repo_url or "") or find_github(text)}


ingest_file = ingest_pdf


def upload_text(pid: str) -> str:
    p = UPLOAD_DIR / f"{pid}.txt"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def parse_arxiv_id(s: str) -> str:
    m = re.search(r"(\d{4}\.\d{4,5})(v\d+)?", s)
    if not m:
        raise ValueError(f"not an arXiv id or URL: {s}")
    return m.group(1)


def fetch_paper(arxiv_id: str) -> dict:
    """One paper by id, with its code link from the abstract/comment, the paper HTML, or Hugging Face."""
    r = _arxiv_get("http://export.arxiv.org/api/query", params={"id_list": arxiv_id, "max_results": 1})
    r.raise_for_status()
    e = ET.fromstring(r.text).find("a:entry", ATOM)
    if e is None or not e.findtext("a:title", "", ATOM).strip():
        raise ValueError(f"arXiv has no paper {arxiv_id}")
    abstract = " ".join((e.findtext("a:summary", "", ATOM) or "").split())
    comment = e.findtext("arxiv:comment", "", ATOM) or ""
    repo = find_github(abstract + " " + comment)
    if not repo:
        try:
            h = requests.get(f"https://huggingface.co/api/papers/{arxiv_id}", headers=UA, timeout=30)
            if h.status_code == 200 and h.json().get("githubRepo"):
                repo = find_github(h.json()["githubRepo"])
        except Exception:
            pass
    if not repo:
        repo = paper_links(arxiv_id)
    return {"arxiv_id": arxiv_id, "title": " ".join((e.findtext("a:title", "", ATOM) or "").split()),
            "abstract": abstract, "comment": comment, "categories": [c.get("term") for c in e.findall("a:category", ATOM)],
            "published": e.findtext("a:published", "", ATOM), "repo_url": repo}


def hf_daily(limit: int = 100) -> list[dict]:
    """Hugging Face Daily Papers: community-curated fresh arXiv papers, many with a
    linked GitHub repo. Author fields are deliberately not stored."""
    r = requests.get("https://huggingface.co/api/daily_papers", params={"limit": limit}, headers=UA, timeout=30)
    r.raise_for_status()
    out = []
    for x in r.json():
        p = x.get("paper", {})
        repo = p.get("githubRepo")
        out.append({"arxiv_id": p.get("id"), "title": " ".join((p.get("title") or "").split()),
                    "abstract": " ".join((p.get("summary") or "").split()), "comment": "",
                    "categories": ["hf-daily"], "published": p.get("publishedAt", ""),
                    "repo_url": find_github(repo) if repo else None})
    return out


def hf_fulltext(arxiv_id: str) -> str:
    r = requests.get(f"https://huggingface.co/papers/{arxiv_id}.md", headers=UA, timeout=60)
    return r.text if r.status_code == 200 and len(r.text) > 3000 else ""


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
    hf = hf_fulltext(arxiv_id)
    if hf:
        return hf, "hf-papers-md"
    r = _arxiv_get(f"https://arxiv.org/abs/{arxiv_id}")
    m = re.search(r'(?s)<blockquote class="abstract[^"]*">(.*?)</blockquote>', r.text)
    return (_strip_html(m.group(1)) if m else ""), "arxiv-abstract"


def paper_links(arxiv_id: str) -> str | None:
    """Look for a GitHub link in the paper HTML when the abstract has none."""
    r = _arxiv_get(f"https://arxiv.org/html/{arxiv_id}")
    if r.status_code != 200:
        return None
    return find_github(r.text)


class RateLimited(RuntimeError):
    """GitHub API quota exhausted. Raised instead of returning the 403, so a stage fails (and is retried later)
    rather than caching "repo unavailable" as if the repository did not exist."""

    def __init__(self, reset: float):
        super().__init__(f"GitHub API rate limit, resets in {max(0, int(reset - time.time()))}s")
        self.reset = reset


@lru_cache(maxsize=1)
def _gh_token() -> str | None:
    # GH_TOKEN (read-only, public repos) or the operator's existing GitHub CLI login, to avoid the
    # 60 req/h anonymous limit. Kept in memory only; never sent to sandboxes.
    if os.getenv("GH_TOKEN"):
        return os.environ["GH_TOKEN"]
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
    r = requests.get(f"https://api.github.com/{path.lstrip('/')}", headers=h, timeout=30)
    if r.status_code in (403, 429) and r.headers.get("X-RateLimit-Remaining") == "0":
        raise RateLimited(float(r.headers.get("X-RateLimit-Reset") or time.time() + 3600))
    return r


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
    manifests: dict[str, str] = {}
    names = ("requirements.txt", "pyproject.toml", "setup.py", "environment.yml", "environment.yaml", "setup.cfg")
    for t in sorted((x for x in tree if x["path"].split("/")[-1] in names), key=lambda x: x["path"].count("/"))[:4]:
        rr2 = requests.get(f"https://raw.githubusercontent.com/{owner}/{name}/{sha}/{t['path']}", headers=UA, timeout=30)
        if rr2.status_code == 200:
            manifests[t["path"]] = rr2.text[:4000]
    return {
        "manifests": manifests,
        "owner": owner, "name": name, "full_name": meta.get("full_name"), "sha": sha, "branch": branch,
        "size_kb": meta.get("size", 0), "language": meta.get("language"), "stars": meta.get("stargazers_count"),
        "archived": meta.get("archived"), "license": (meta.get("license") or {}).get("spdx_id"),
        "pushed_at": meta.get("pushed_at"), "tree": tree, "readme": readme,
    }
