"""neatlogs setup. One WORKFLOW root per paper, one CHAIN/AGENT span per stage."""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from typing import Any, Iterator

import neatlogs
from opentelemetry import trace as otel_trace

_initialized = False


def init(run_label: str, variant: str) -> None:
    global _initialized
    if _initialized:
        return
    neatlogs.init(
        api_key=os.environ.get("NEATLOGS_API_KEY"),
        endpoint=os.environ.get("NEATLOGS_ENDPOINT", "https://ingest.neatlogs.com"),
        workflow_name="paperci-reproduce-paper",
        tags=["paperci", f"run:{run_label}", f"variant:{variant}"],
        capture_logs=True,
        flush_interval=2.0,
    )
    _initialized = True


def shutdown() -> None:
    if _initialized:
        neatlogs.flush()
        neatlogs.shutdown()


_root_stack: list[Any] = []


def trace_id_of(s: Any) -> str | None:
    try:
        tid = s.get_span_context().trace_id
        return format(tid, "032x") if tid else None
    except Exception:
        return None


def current_trace_id() -> str | None:
    # neatlogs keeps its own private provider/context, so the global OTel
    # current span is not reliable; fall back to the innermost span we opened.
    ctx = otel_trace.get_current_span().get_span_context()
    if ctx and ctx.trace_id:
        return format(ctx.trace_id, "032x")
    return trace_id_of(_root_stack[-1]) if _root_stack else None


def _safe(v: Any) -> Any:
    if isinstance(v, (str, int, float, bool)):
        return v
    return json.dumps(v, default=str)[:4000]


@contextmanager
def span(name: str, kind: str = "CHAIN", **attrs: Any) -> Iterator[Any]:
    """Open a span; attributes are stamped as paperci.<key>."""
    with neatlogs.trace(name, kind=kind) as s:
        for k, v in attrs.items():
            set_attr(s, k, v)
        _root_stack.append(s)
        try:
            yield s
        finally:
            _root_stack.pop()


def set_attr(s: Any, key: str, value: Any) -> None:
    try:
        s.set_attribute(f"paperci.{key}", _safe(value))
    except Exception:
        pass


def log(msg: str, level: str = "info", **data: Any) -> None:
    try:
        neatlogs.log(msg, level=level, **{k: _safe(v) for k, v in data.items()})
    except Exception:
        pass
