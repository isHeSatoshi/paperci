# neatlogs feedback from building PaperCI (for the neatlogs team / Discord)

SDK `neatlogs` 1.4.26 (Python, `neatlogs.wrap(OpenAI(...))` + `neatlogs.trace(...)` spans), CLI `neatlogs-cli`
with an OAuth device login, project on the free tier. All observations below were read back with the CLI.

## 1. Traces stuck in `dlq` (finalization failed)
16 of the ~55 PaperCI traces have `finalizationStatus: dlq`. `traces spans list` and `traces raw-spans` return
`TRACE_FINALIZATION_FAILED` ("The trace could not be finalized for span projection"). Their `traceName` is the
name of some child span (`tool:fetch_repo`, `openai.chat.completions.create`, a LOG message), which suggests
finalization never reached the root WORKFLOW span.

Examples: `14ed5fe7f083b2b1ff4c98fbbf705223` (1 min run), `743e966776a924a24e662d288bc41105` (~5 min),
`7bfd8f9e85e8a9e5a02622e3484c574c` (744 s), plus short reverify traces (`b45e0b12b3262e68989058d7f2c2d823`).
Not duration-driven: a 1550 s trace with 63 spans (`4e85d604ac7a5d343950ee923f573502`) finalized fine, and
1-minute traces went to `dlq`. No exporter errors were logged on the client
(`[neatlogs] oversized span rejected` never appeared).

Update 2026-10-11 ~10:35 IST: `python -m neatlogs doctor --probe` now FAILS with `TRACE_READBACK_TIMEOUT`
("Ingestion: processing at pii_redaction"); the same probe passed all 8 checks on 2026-10-10. A run that
finished at 10:28 IST (`c07db8e5e1be1fb9872ccdf8636faf6c`) had not appeared in `traces list` 10 minutes later.
So at least part of the DLQ / missing-trace behaviour looks like an ingestion backlog on the server side.

## 2. A finalized long trace with zero LLM calls counted
`a9540916517fe38646791aacb7bade88` (723 s, 18 builder turns with LLM calls) is `finalized` with
`llmCallsCount: 0, toolCallsCount: 0, totalTokens: 0`.

## 3. `costUsd` is 0 for LLM spans behind a LiteLLM proxy
The model names (`claude-sonnet-4.6`, `nova-pro`) are not in the price table, so cost is 0. We carry cost as
`paperci.cost_usd` attributes from our own ledger. A per-project custom price map would solve it.

## 4. LOG span names show the unformatted template
`neatlogs.log("llm cost {stage} {model} ${cost:.5f}", stage=..., ...)` shows the template as the span name in
readback, so many LOG spans share one name.

## 5. CLI OAuth scope
The device login granted read scopes only; `traces update-metadata` and `comments create` return `FORBIDDEN`.
`auth login --scope` exists, but the needed scope name (`observability:write`) is only discoverable in the
bundled OpenAPI. Public trace sharing is read-only in the CLI (`sharing traces get`), so share links need the UI.

## What worked well
`doctor --probe` (all 8 checks pass), `wrap()` on an OpenAI-compatible client with no code changes, GUARDRAIL
spans with `neatlogs.guardrail.*` attributes, tags for run labels, and CLI readback for automated evidence.
