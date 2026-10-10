"""Spend ledger: tokens x price per call, per-paper caps and a global stop."""
from __future__ import annotations

import os
import time

import requests

from . import db
from .config import (DEMO_RESERVE_USD, GLOBAL_BUDGET_USD, HARD_STOP_USD, LITELLM_BASE_URL,
                     PER_PAPER_CAP_USD, PRICES)


class BudgetExceeded(RuntimeError):
    """Raised before a call that would cross a budget line."""


_remote_cache: dict[str, float] = {"ts": 0.0, "spend": 0.0}


def price_call(model: str, prompt_tokens: int, completion_tokens: int,
               cache_read: int = 0, cache_write: int = 0) -> float:
    # Unknown models are priced as the most expensive allowed model (conservative).
    p = PRICES.get(model) or PRICES["claude-sonnet-4.6"]
    uncached = max(prompt_tokens - cache_read - cache_write, 0)
    return (uncached * p["in"] + completion_tokens * p["out"]
            + cache_read * p.get("cache_read", p["in"]) + cache_write * p.get("cache_write", p["in"]))


def record(arxiv_id: str, stage: str, run_label: str, model: str, prompt_tokens: int,
           completion_tokens: int, cache_read: int = 0, cache_write: int = 0) -> float:
    cost = price_call(model, prompt_tokens, completion_tokens, cache_read, cache_write)
    db.ex("INSERT INTO spend (ts,arxiv_id,stage,run_label,model,prompt_tokens,completion_tokens,"
          "cache_read_tokens,cache_write_tokens,cost_usd) VALUES (?,?,?,?,?,?,?,?,?,?)",
          (time.time(), arxiv_id, stage, run_label, model, prompt_tokens, completion_tokens,
           cache_read, cache_write, cost))
    return cost


def local_total() -> float:
    r = db.q1("SELECT COALESCE(SUM(cost_usd),0) AS s FROM spend")
    return float(r["s"]) if r else 0.0


def paper_total(arxiv_id: str, run_label: str | None = None, stage: str | None = None) -> float:
    sql, args = "SELECT COALESCE(SUM(cost_usd),0) AS s FROM spend WHERE arxiv_id=?", [arxiv_id]
    if run_label:
        sql += " AND run_label=?"; args.append(run_label)
    if stage:
        sql += " AND stage=?"; args.append(stage)
    r = db.q1(sql, tuple(args))
    return float(r["s"]) if r else 0.0


def remote_spend(max_age: float = 300.0) -> float:
    """Authoritative spend reported by the LiteLLM proxy for this key."""
    if time.time() - _remote_cache["ts"] < max_age:
        return _remote_cache["spend"]
    base = LITELLM_BASE_URL.rstrip("/")
    base = base[:-3] if base.endswith("/v1") else base
    try:
        r = requests.get(f"{base}/key/info", timeout=15,
                         headers={"Authorization": "Bearer " + os.environ["LITELLM_API_KEY"]})
        r.raise_for_status()
        _remote_cache.update(ts=time.time(), spend=float(r.json()["info"].get("spend") or 0.0))
    except Exception:
        _remote_cache["ts"] = time.time()
    return _remote_cache["spend"]


def global_spend() -> float:
    return max(local_total(), remote_spend())


def precheck(arxiv_id: str, run_label: str, est_cost: float, stage_cap: float | None = None,
             stage: str | None = None, allow_reserve: bool = False) -> None:
    spent = global_spend()
    limit = min(HARD_STOP_USD, GLOBAL_BUDGET_USD - (0 if allow_reserve else DEMO_RESERVE_USD))
    if spent + est_cost > limit:
        raise BudgetExceeded(f"global spend {spent:.3f}+{est_cost:.3f} > limit {limit:.2f}")
    if arxiv_id and arxiv_id != "-":
        p = paper_total(arxiv_id, run_label)
        if p + est_cost > PER_PAPER_CAP_USD:
            raise BudgetExceeded(f"paper spend {p:.3f}+{est_cost:.3f} > cap {PER_PAPER_CAP_USD:.2f}")
        if stage_cap is not None and stage:
            s = paper_total(arxiv_id, run_label, stage)
            if s + est_cost > stage_cap:
                raise BudgetExceeded(f"stage {stage} spend {s:.3f}+{est_cost:.3f} > cap {stage_cap:.2f}")


def summary() -> dict:
    rows = db.q("SELECT stage, model, COUNT(*) n, SUM(prompt_tokens) pt, SUM(completion_tokens) ct, "
                "SUM(cost_usd) c FROM spend GROUP BY stage, model ORDER BY c DESC")
    return {"local_total": local_total(), "remote_total": remote_spend(0),
            "by_stage_model": [dict(r) for r in rows]}
