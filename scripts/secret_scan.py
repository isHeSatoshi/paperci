"""Count occurrences of the real secret values (from .env) in files, without printing them.

  python scripts/secret_scan.py <file-or-dir> [...]
  python scripts/secret_scan.py --git        # working tree + full git history
Exit code 1 if any secret is found."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
names = ("NEATLOGS_API_KEY", "LITELLM_API_KEY", "CRUSTDATA_API_KEY", "FACTORY_API_KEY")
secrets = {}
for line in (ROOT / ".env").read_text().splitlines():
    k, _, v = line.partition("=")
    if k in names and len(v) >= 12:
        secrets[k] = v.encode()
try:
    tok = subprocess.run(["gh", "auth", "token"], capture_output=True, timeout=15).stdout.strip()
    if len(tok) > 12:
        secrets["GH_TOKEN"] = tok
except Exception:
    pass


def scan_bytes(label: str, data: bytes) -> int:
    hits = 0
    for k, v in secrets.items():
        n = data.count(v)
        if n:
            print(f"FOUND {k} x{n} in {label}")
            hits += n
    return hits


total = 0
if sys.argv[1:] == ["--git"]:
    tracked = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True).stdout.split(b"\0")
    for f in tracked:
        if f:
            p = ROOT / f.decode()
            if p.is_file():
                total += scan_bytes(f"worktree:{f.decode()}", p.read_bytes())
    revs = subprocess.run(["git", "rev-list", "--all"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    for rev in revs:
        diff = subprocess.run(["git", "show", "--format=%B", "-p", rev], cwd=ROOT, capture_output=True).stdout
        total += scan_bytes(f"commit:{rev[:10]}", diff)
    refs = subprocess.run(["git", "for-each-ref", "--format=%(objectname)"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    objs = subprocess.run(["git", "rev-list", "--objects", "--all"], cwd=ROOT, capture_output=True, text=True).stdout.split("\n")
    for line in objs:
        sha = line.split(" ")[0]
        if sha:
            blob = subprocess.run(["git", "cat-file", "-p", sha], cwd=ROOT, capture_output=True).stdout
            total += scan_bytes(f"object:{sha[:10]}", blob)
    print(f"scanned {len(tracked)} tracked files, {len(revs)} commits, {len(objs)} objects")
else:
    for arg in sys.argv[1:]:
        p = Path(arg)
        files = [p] if p.is_file() else [x for x in p.rglob("*") if x.is_file()]
        for f in files:
            total += scan_bytes(str(f), f.read_bytes())
print("secrets checked:", sorted(secrets), "hits:", total)
sys.exit(1 if total else 0)
