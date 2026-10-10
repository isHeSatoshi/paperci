"""SQLite state store. Every stage persists its output so a crash resumes."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator

from .config import DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
  arxiv_id TEXT PRIMARY KEY,
  title TEXT, abstract TEXT, categories TEXT, published TEXT,
  repo_url TEXT, split TEXT, status TEXT DEFAULT 'new',
  verdict TEXT, confidence TEXT, trace_id TEXT,
  created_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS stages (
  arxiv_id TEXT, stage TEXT, run_label TEXT,
  status TEXT, attempts INTEGER DEFAULT 0,
  output TEXT, error TEXT, started_at REAL, finished_at REAL,
  cost_usd REAL DEFAULT 0, trace_id TEXT,
  PRIMARY KEY (arxiv_id, stage, run_label)
);
CREATE TABLE IF NOT EXISTS sealed_claims (
  arxiv_id TEXT PRIMARY KEY, sha256 TEXT, payload TEXT, created_at REAL
);
CREATE TABLE IF NOT EXISTS spend (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, arxiv_id TEXT, stage TEXT,
  run_label TEXT, model TEXT, prompt_tokens INTEGER, completion_tokens INTEGER,
  cache_read_tokens INTEGER, cache_write_tokens INTEGER, cost_usd REAL
);
CREATE TABLE IF NOT EXISTS failures (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, arxiv_id TEXT, run_label TEXT,
  stage TEXT, category TEXT, detail TEXT, recovered INTEGER DEFAULT 0, trace_id TEXT
);
CREATE TABLE IF NOT EXISTS package_installs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, arxiv_id TEXT, package TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, arxiv_id TEXT, run_label TEXT,
  kind TEXT, payload TEXT
);
"""

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(_SCHEMA)
    return _conn


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    with _lock:
        c = conn()
        c.execute("BEGIN")
        try:
            yield c
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise


def q(sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    with _lock:
        return conn().execute(sql, args).fetchall()


def q1(sql: str, args: tuple = ()) -> sqlite3.Row | None:
    rows = q(sql, args)
    return rows[0] if rows else None


def ex(sql: str, args: tuple = ()) -> None:
    with _lock:
        conn().execute(sql, args)


def upsert_paper(p: dict[str, Any]) -> bool:
    """Insert a paper if new. Returns True when inserted."""
    now = time.time()
    with tx() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO papers (arxiv_id,title,abstract,categories,published,repo_url,split,status,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (p["arxiv_id"], p.get("title"), p.get("abstract"), json.dumps(p.get("categories", [])),
             p.get("published"), p.get("repo_url"), p.get("split"), "new", now, now),
        )
        return cur.rowcount > 0


def set_paper(arxiv_id: str, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = time.time()
    cols = ",".join(f"{k}=?" for k in fields)
    ex(f"UPDATE papers SET {cols} WHERE arxiv_id=?", (*fields.values(), arxiv_id))


def get_stage(arxiv_id: str, stage: str, run_label: str) -> sqlite3.Row | None:
    return q1("SELECT * FROM stages WHERE arxiv_id=? AND stage=? AND run_label=?", (arxiv_id, stage, run_label))


def save_stage(arxiv_id: str, stage: str, run_label: str, **fields: Any) -> None:
    row = get_stage(arxiv_id, stage, run_label)
    if "output" in fields and not isinstance(fields["output"], (str, type(None))):
        fields["output"] = json.dumps(fields["output"], default=str)
    if row is None:
        fields.setdefault("started_at", time.time())
        cols = ["arxiv_id", "stage", "run_label", *fields.keys()]
        ex(f"INSERT INTO stages ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
           (arxiv_id, stage, run_label, *fields.values()))
    else:
        cols = ",".join(f"{k}=?" for k in fields)
        ex(f"UPDATE stages SET {cols} WHERE arxiv_id=? AND stage=? AND run_label=?",
           (*fields.values(), arxiv_id, stage, run_label))


def log_event(arxiv_id: str, run_label: str, kind: str, payload: Any) -> None:
    ex("INSERT INTO events (ts,arxiv_id,run_label,kind,payload) VALUES (?,?,?,?,?)",
       (time.time(), arxiv_id, run_label, kind, json.dumps(payload, default=str)[:20000]))


def log_failure(arxiv_id: str, run_label: str, stage: str, category: str, detail: str,
                recovered: bool = False, trace_id: str | None = None) -> int:
    with tx() as c:
        cur = c.execute(
            "INSERT INTO failures (ts,arxiv_id,run_label,stage,category,detail,recovered,trace_id) VALUES (?,?,?,?,?,?,?,?)",
            (time.time(), arxiv_id, run_label, stage, category, detail[:2000], int(recovered), trace_id))
        return int(cur.lastrowid)


def mark_recovered(failure_id: int) -> None:
    ex("UPDATE failures SET recovered=1 WHERE id=?", (failure_id,))
