# How PaperCI improves itself (and why it can't cheat)

## The loop
1. **Sense (neatlogs).** Every paper is one WORKFLOW trace; the builder is an AGENT span with LLM and TOOL
   children; gates are GUARDRAIL spans; failures are LOG spans with a failure class. Raw sandbox command
   logs are kept next to the trace ID.
2. **Diagnose.** `paperci/improver.py` rebuilds the failure taxonomy from raw logs with the current
   classifier, so classifier fixes apply to old evidence. It adds outcome-level classes:
   `turn_budget_exhausted`, `late_give_up`, `egress_denied`.
3. **Propose.** An improver agent (Claude Sonnet) sees failure clusters, what the builder did next, which
   playbook generation produced each failure, the existing rules and the current egress allowlist. It
   proposes recovery rules (regex + advice) and optionally a controller (enforced knobs).
4. **Gate (deterministic, no LLM).**
   - A rule's regex must match real failures of its own class (recall) and at most 25% of other classes.
   - Advice must not mention hardcoding, faking, copying results, skipping verification, or guessing.
   - Advice must not reference hosts outside the allowlist.
   - **Staleness:** an egress rule claiming a host is blocked is rejected if that host is allowlisted now.
   - Controller knobs must be ordered and within bounds, and the projected worst-case builder cost must fit the cap.
5. **Adopt.** A new generation is written to `data/playbook.json`, history goes to `data/playbook_history/genN.json`,
   and it is committed with an Entire checkpoint so the reasoning travels with the change.
6. **Environment learning.** Packages installed by builders in at least 2 different papers are
   install-tested in a fresh sandbox and promoted into the base image (`paperci/sandbox:genN`).
7. **Measure on held-out.** The improver only ever reads dev-split evidence. Held-out papers are run with
   v1 (gen 0) and v2 (latest gen) on the same sealed claims.

## What happened in this hackathon (measured)
- **gen1** (from 7 dev attempts, 0 built): 8 proposals. 4 accepted. 4 rejected: 2 were stale (they said
  openml.org and api.github.com were blocked, but those had been allowlisted after the same traces), and 2
  had regexes that matched none of their own failures.
- **gen1 on dev:** no behavioral change. Both dev papers still ran out of turns, reading code.
- **gen2** (from gen0 + gen1 evidence): the improver escalated from advice to an **enforced controller**
  (execute repo code by turn 5, write the run script by turn 8, single target). The gate projected a
  worst-case builder cost of $0.30. One rule was rejected by the integrity filter for telling the builder
  to "commit to a best-guess implementation".
- **gen2 on dev:** the copula-VI paper went from max-turns (gen1) to built in 10 turns. It was
  oracle-validated and packaged.
- **Held-out before/after:** see `docs/evidence.md` section 1. Builds 2/5 -> 3/5, turns 16.2 -> 10.2,
  $/attempt 0.255 -> 0.182, validated tasks 0 -> 1.

## Optimisation pressure finds shortcuts: the self-audit
A builder rewarded for "submit a run that prints the metric" learned shortcuts that passed the original
verifier. Auditing the held-out v2 builds by hand found that 2 of 3 were not faithful: one replayed a
precomputed results pickle (in v1 too), and one computed the metric with a formula the builder wrote on a
data file, never calling repo code. This is the expected failure mode of a self-improving system, and it is
why the judge must be outside the loop.

The fix was made by the engineer, not by the improver (the verifier is a frozen surface for the loop):
`precomputed_results_replay` and `no_repo_code_path` integrity flags, claim-id disambiguation for claims
sharing a metric, header-aware and whitespace-tolerant extraction. Then `scripts/reverify.py` re-judged
**every** stored run (dev and held-out, v1 and v2) with identical new rules, and invalidated 3 packages.
Reported numbers use the stricter judge, so "faithful measured runs" stays 1 -> 1 on held-out: the honest
gain is speed, cost and one validated task, not more faithful reproductions.

The harness heals itself too (`ensure_infra()` before every sandbox and after crashes): it recreates the
internal network, restarts a dead proxy (shown by the chaos run), removes containers orphaned by a daemon
crash, and warns on a low host disk. Container deaths during the oracle are retried as infra failures
instead of being counted against the task.

## Frozen surfaces (never modified by the loop)
Verifier code and tolerance rules, claim extraction and sealing, the held-out split, integrity checks,
and the egress allowlist. Allowlist additions are security policy and need a human (they were made by the
engineer, citing trace evidence, and applied to both v1 and v2).

## Why this matters beyond reproducibility
The same loop applies to any agent that builds environments. As simulators get better, PaperCI's builder
can target simulation-based papers. The learned playbook and image carry over, so each new domain starts
from what earlier papers taught it.
