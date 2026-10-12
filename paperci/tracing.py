"""neatlogs setup. One WORKFLOW root per paper, one CHAIN/AGENT span per stage."""
from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Iterator

import neatlogs
from opentelemetry import trace as otel_trace

_initialized = False
_session: str | None = None


def init(run_label: str, variant: str) -> None:
    global _initialized, _session
    if _initialized:
        return
    # A run label is the natural session: one web job, one autopilot day, one eval batch.
    _session = os.environ.get("PAPERCI_SESSION") or f"run:{run_label}"
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
        bad = delivery_problems()
        if bad:
            try:
                from . import ops
                ops.event("incident", f"Telemetry delivery problems in this run: {bad}", component="neatlogs")
            except Exception:
                pass
        neatlogs.shutdown()


def delivery_problems() -> dict:
    """Non-zero drop/failure counters from the SDK; empty means every span and log was delivered."""
    try:
        d = neatlogs.get_delivery_diagnostics()
    except Exception:
        return {}
    return {k: v for k, v in d.items() if isinstance(v, int) and not isinstance(v, bool) and v
            and any(w in k for w in ("drops", "failures"))}


_PROMPT_CACHE = Path(__file__).resolve().parent.parent / "data" / "neatlogs_prompts.json"


def register_prompt(name: str, text: str, label: str = "", tags: tuple[str, ...] = (), note: str = "",
                    config: dict | None = None) -> int | None:
    """Store `text` as a versioned neatlogs prompt, once per distinct content. Never blocks a run for long
    or fails it: prompt management is bookkeeping, not a dependency. neatlogs accepts at most one label per
    version (a ValueError otherwise), so anything else goes in tags."""
    if not _initialized or not os.environ.get("NEATLOGS_API_KEY"):
        return None
    sha = hashlib.sha256(text.encode()).hexdigest()[:16]
    try:
        cache = json.loads(_PROMPT_CACHE.read_text(encoding="utf-8"))
    except Exception:
        cache = {}
    if sha in cache.get(name, {}):
        return cache[name][sha]

    def create() -> int:
        h = neatlogs.create_prompt(name=name, prompt=text, labels=[label] if label else [],
                                   tags=["paperci", *tags],
                                   config={**(config or {}), "sha": sha}, commit_message=note or None)
        return int(h.version)
    ex = ThreadPoolExecutor(max_workers=1)
    try:
        version = ex.submit(create).result(timeout=15)
    except Exception as e:
        log("prompt registration skipped: {err}", level="warning", err=f"{type(e).__name__}: {e}"[:200])
        return None
    finally:
        ex.shutdown(wait=False)
    cache.setdefault(name, {})[sha] = version
    try:
        _PROMPT_CACHE.parent.mkdir(parents=True, exist_ok=True)
        _PROMPT_CACHE.write_text(json.dumps(cache, indent=1), encoding="utf-8")
    except Exception:
        pass
    return version


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
def span(name: str, kind: str = "CHAIN", prompt: dict | None = None, inp: Any = None,
         **attrs: Any) -> Iterator[Any]:
    """Open a span; attributes are stamped as paperci.<key>. `prompt` passes neatlogs prompt-template fields
    (system_prompt_template, version, ...) so LLM calls inside are linked to a prompt version. `inp` becomes
    the span input; without one neatlogs shows the first captured log line as the trace input. An exception
    leaving the span marks it ERROR (neatlogs.trace does not)."""
    nl: dict = dict(prompt or {})
    stack = ExitStack()
    if kind == "WORKFLOW":
        who = {"session_id": _session, "end_user_id": os.environ.get("PAPERCI_END_USER") or "operator"}
        nl.update(who)
        stack.enter_context(neatlogs.identify(**who))
    with stack, neatlogs.trace(name, kind=kind, **nl) as s:
        if kind == "TOOL":
            s.set_attribute("neatlogs.tool.name", name.split(":", 1)[-1])
        for k, v in attrs.items():
            set_attr(s, k, v)
        if inp is not None:
            io(s, inp=inp)
        _root_stack.append(s)
        try:
            yield s
        except BaseException as e:
            if not isinstance(e, GeneratorExit):
                fail(s, f"{type(e).__name__}: {e}")
                try:
                    s.record_exception(e)
                except Exception:
                    pass
            raise
        finally:
            _root_stack.pop()


def set_attr(s: Any, key: str, value: Any) -> None:
    try:
        s.set_attribute(f"paperci.{key}", _safe(value))
    except Exception:
        pass


def _text(v: Any, limit: int = 8000) -> str:
    return (v if isinstance(v, str) else json.dumps(v, default=str))[:limit]


def io(s: Any, inp: Any = None, out: Any = None) -> None:
    """Span input/output in the neatlogs convention (input.value/output.value), which is what the trace list,
    detections and evaluations read."""
    try:
        if inp is not None:
            s.set_attribute("input.value", _text(inp))
        if out is not None:
            s.set_attribute("output.value", _text(out))
    except Exception:
        pass


def fail(s: Any, reason: str) -> None:
    """Mark a span ERROR so the platform's status-based detections ("Execution Failed") fire."""
    try:
        from opentelemetry.trace import Status, StatusCode
        s.set_status(Status(StatusCode.ERROR, reason[:500]))
        s.set_attribute("paperci.failure", reason[:500])
    except Exception:
        pass


def guardrail(s: Any, checked: Any, passed: bool, output: Any) -> None:
    """Documented GUARDRAIL attributes so gates render as Passed/Blocked."""
    try:
        s.set_attribute("neatlogs.guardrail.input", _safe(checked))
        s.set_attribute("neatlogs.guardrail.passed", bool(passed))
        s.set_attribute("neatlogs.guardrail.output", _safe(output))
    except Exception:
        pass


def log(msg: str, level: str = "info", **data: Any) -> None:
    try:
        neatlogs.log(msg, level=level, **{k: _safe(v) for k, v in data.items()})
    except Exception:
        pass
