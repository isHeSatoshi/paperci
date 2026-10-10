"""OpenAI-compatible client (LiteLLM proxy) wrapped by neatlogs, with spend
accounting, bounded retries, and typed (Pydantic) JSON outputs."""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Type, TypeVar

import neatlogs
import openai
from openai import OpenAI
from pydantic import BaseModel, ValidationError

from . import ledger, tracing
from .config import FORBIDDEN_MODELS, LITELLM_BASE_URL, LLM_MAX_RETRIES, PRICES

T = TypeVar("T", bound=BaseModel)
_client: OpenAI | None = None


def client() -> OpenAI:
    global _client
    if _client is None:
        _client = neatlogs.wrap(OpenAI(base_url=LITELLM_BASE_URL, api_key=os.environ["LITELLM_API_KEY"],
                                       max_retries=0, timeout=180))
    return _client


def _chars(messages: list[dict]) -> int:
    n = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            n += len(c)
        elif isinstance(c, list):
            n += sum(len(b.get("text", "")) for b in c if isinstance(b, dict))
        if m.get("tool_calls"):
            n += len(json.dumps(m["tool_calls"]))
    return n


def estimate(model: str, messages: list[dict], max_tokens: int, tools: list | None = None) -> float:
    p = PRICES.get(model) or PRICES["claude-sonnet-4.6"]
    tin = (_chars(messages) + (len(json.dumps(tools)) if tools else 0)) / 3.2
    return tin * p["in"] + min(max_tokens, 2000) * p["out"]


def _with_cache_marks(messages: list[dict]) -> list[dict]:
    """Anthropic prompt caching: mark the system prompt and the newest message."""
    out = [dict(m) for m in messages]
    idxs = [0] if out and out[0]["role"] == "system" else []
    if len(out) > 1 and out[-1]["role"] in ("user", "tool"):
        idxs.append(len(out) - 1)
    for i in idxs:
        c = out[i].get("content")
        if isinstance(c, str) and c:
            out[i]["content"] = [{"type": "text", "text": c, "cache_control": {"type": "ephemeral"}}]
    return out


def chat(*, arxiv_id: str, stage: str, run_label: str, model: str, messages: list[dict],
         tools: list | None = None, max_tokens: int = 1500, temperature: float = 0.0,
         stage_cap: float | None = None, cache: bool = False, allow_reserve: bool = False) -> Any:
    if model in FORBIDDEN_MODELS:
        raise ValueError(f"model {model} is disabled by budget policy")
    est = estimate(model, messages, max_tokens, tools)
    ledger.precheck(arxiv_id, run_label, est, stage_cap=stage_cap, stage=stage, allow_reserve=allow_reserve)
    send = _with_cache_marks(messages) if cache and model.startswith("claude") else messages
    kwargs: dict[str, Any] = dict(model=model, messages=send, max_tokens=max_tokens, temperature=temperature)
    if tools:
        kwargs["tools"] = tools
    last_err: Exception | None = None
    for attempt in range(LLM_MAX_RETRIES + 1):
        try:
            resp = client().chat.completions.create(**kwargs)
            break
        except (openai.RateLimitError, openai.APIConnectionError, openai.APITimeoutError,
                openai.InternalServerError) as e:
            last_err = e
            tracing.log("llm retry {attempt} after {err}", level="warning", attempt=attempt,
                        err=type(e).__name__, model=model, stage=stage)
            time.sleep(min(30, 3 * 2 ** attempt))
    else:
        raise RuntimeError(f"LLM call failed after retries: {type(last_err).__name__}")
    u = resp.usage
    pt = int(getattr(u, "prompt_tokens", 0) or 0)
    ct = int(getattr(u, "completion_tokens", 0) or 0)
    cr = int(getattr(u, "cache_read_input_tokens", 0) or 0)
    cw = int(getattr(u, "cache_creation_input_tokens", 0) or 0)
    if not cr:
        det = getattr(u, "prompt_tokens_details", None)
        cr = int(getattr(det, "cached_tokens", 0) or 0) if det else 0
    cost = ledger.record(arxiv_id, stage, run_label, model, pt, ct, cr, cw)
    tracing.log("llm cost {stage} {model} ${cost:.5f}", stage=stage, model=model, cost=cost,
                prompt_tokens=pt, completion_tokens=ct, cache_read=cr, cache_write=cw)
    return resp


_JSON_RE = re.compile(r"\{.*\}|\[.*\]", re.S)


def extract_json(text: str) -> Any:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = _JSON_RE.search(text)
        if not m:
            raise
        return json.loads(m.group(0))


def chat_typed(schema: Type[T], *, system: str, user: str, max_tokens: int = 1500,
               repair_attempts: int = 1, **kw: Any) -> T:
    """Ask for JSON matching `schema`; on parse/validation error, send the error back once."""
    shape = json.dumps(schema.model_json_schema())[:3000]
    messages = [
        {"role": "system", "content": system + "\n\nReply with ONLY a JSON object matching this JSON schema:\n" + shape},
        {"role": "user", "content": user},
    ]
    for attempt in range(repair_attempts + 1):
        resp = chat(messages=messages, max_tokens=max_tokens, **kw)
        text = resp.choices[0].message.content or ""
        try:
            return schema.model_validate(extract_json(text))
        except (ValidationError, json.JSONDecodeError, ValueError) as e:
            tracing.log("typed output invalid (attempt {attempt}): {err}", level="warning",
                        attempt=attempt, err=str(e)[:300])
            if attempt >= repair_attempts:
                raise
            messages += [{"role": "assistant", "content": text},
                         {"role": "user", "content": f"That was not valid. Error: {str(e)[:500]}. Reply with only the corrected JSON."}]
    raise RuntimeError("unreachable")
