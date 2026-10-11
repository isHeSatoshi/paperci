# Self-improvement that is tested before it is trusted

PaperCI's builder agent improves itself every night. This page describes what it may change, how a change is
judged, and what it can never touch. Code: `paperci/evolve.py`, `paperci/policy_sandbox.py`,
`paperci/stages/builder_policy.py`.

## What changes (the mutable surface)

| Level | What | How it goes live |
|---|---|---|
| 1 | Playbook: recovery rules + controller knobs (turn budget, milestones), shown to the builder as data | automatically, if it wins on the benchmark |
| 2 | The builder's control policy, a Python file (`builder_policy.py`) deciding when to inject mandatory instructions, the late-turn nudge and an extra system-prompt section | only after a human runs `python -m paperci.evolve approve <id>` |
| meta | The improver's own strategy (one instruction added to its prompt) | scored by how much its proposals actually helped; a fixed meta prompt writes new strategies from that scoreboard (recursion depth 1) |

## What never changes (the referee)

The verifier, claim sealing and sealed values, tolerances, the cost ledger, the pipeline, the improver's gate,
the policy sandbox and `evolve.py` itself. Their hashes (plus a hash over all sealed claims) are pinned when a
round starts and re-checked after every evaluation run; any difference voids the round. Held-out papers are
never used for learning or for the benchmark.

## One round (nightly, one step per worker slot, author submissions always first)

1. **Canary**: the last adopted change is compared with live autopilot builds before it; clearly worse
   (build rate down by more than a third) means automatic rollback from the backup.
2. **Meta**: pick a strategy by UCB1 over measured gains; once two strategies have scores, the meta prompt
   proposes a new one (integrity-filtered).
3. **Propose**: candidate A (playbook) from the improver; candidate B (policy) written by the LLM.
4. **Static gates**: rule regex precision/recall on real failures, integrity filter, allowlisted hosts,
   controller cost bound; for policies an AST contract (no imports, no I/O, no dunder access, no while loops,
   only contract state keys, restricted builtins) plus a contract test on 240 synthetic builder states (types, length, determinism, speed).
5. **Evaluate**: real builds + one offline verification run (no network) of the incumbent and each candidate
   on a fixed dev benchmark (`PAPERCI_EVOLVE_BENCH`). These runs never change a paper's public verdict.
6. **Decide**: fitness per paper = 2·built + 1·offline run ok + metrics found (≤3) + 2·claims within tolerance
   − 0.05·turns − 2·cost. Adopt only if the total beats the incumbent by ≥ 1.0, no paper regresses from built
   to not built, and no run raised an integrity flag (a tripwire: a flagged candidate is rejected however high
   it scores, and logged as an incident).

## Bounds

- Budget: `PAPERCI_EVOLVE_BUDGET` (default $1.50) per round, one round per 20 h; evaluation runs are exempt
  from the autopilot's daily pacing but not from the per-paper caps or the global hard stop.
- Poison guards: each benchmark run is tried at most twice; a step that crashes 3 times voids the round.
- Kill switch: `PAPERCI_EVOLVE=0` falls back to the old playbook learner; `reject <id>` drops a candidate.
- Everything is traced (neatlogs session `run:evo-<round>`, builder traces carry `policy_id` and the playbook
  generation) and every decision is an event on the public board.

## Honest limits

- The benchmark is 2 papers, so one round is a noisy signal. That is why playbook adoption needs a margin,
  code adoption needs a human, and every adoption is re-checked on live papers.
- The policy sandbox is defence in depth (AST allowlist + restricted builtins + frozen state copies), not a
  hard security boundary; the human approval step exists because of that.
- Depth 1 only: the meta prompt that writes improver strategies is itself fixed.

## Commands (on the server, in `~/paperci`)

    .venv/bin/python -m paperci.evolve status      # current round, strategies, adoptions
    .venv/bin/python -m paperci.evolve pending     # evolved policies waiting for approval (with file path)
    .venv/bin/python -m paperci.evolve approve <id> | reject <id>
