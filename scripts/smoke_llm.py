"""Smoke test: one traced nova-micro call, ledger record, trace id readout."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from paperci import ledger, llm, tracing  # noqa: E402

tracing.init("smoke", "v0")
with tracing.span("paperci:smoke", kind="WORKFLOW", purpose="smoke") as s:
    tid = tracing.current_trace_id()
    with tracing.span("stage:hello", kind="CHAIN"):
        r = llm.chat(arxiv_id="-", stage="smoke", run_label="smoke", model="nova-micro",
                     messages=[{"role": "user", "content": "Reply with the single word: ok"}], max_tokens=5)
        print("reply:", (r.choices[0].message.content or "").strip())
print("trace_id:", tid)
print("ledger:", round(ledger.local_total(), 6))
tracing.shutdown()
