# PROGRESS (resume point for any new session)

## State (2026-10-11 ~01:30 IST)
- Pipeline code complete end to end: watcher -> triage -> sealed claims -> builder (v1/v2) -> offline verifier ->
  robustness -> verdict -> Harbor packager -> oracle replay -> trainability (cheap solver) -> registry/dashboard.
- Self-improvement code: `python -m paperci improve --labels dev-v1 --apply` (playbook generations in data/playbook.json),
  `python -m paperci envlearn --apply` (base image promotions).
- Data: 145 papers watched, 55 triage-accepted, claims grounded + sealed. Dev baseline v1 running/finished on ~7 dev papers.
- Spend so far: see `python -m paperci budget` (about $2 at this point).

## How to resume
1. Docker: `Invoke-CimMethod Win32_Process Create` with Docker Desktop.exe if `docker version` fails (do not use Start-Process from the agent shell).
2. `python -m paperci stats` for the funnel; `scripts/show_queue.py <split> <label> <n>` for the next papers.
3. Long runs: `scripts/run_bg.ps1 -Name X -CliArgs "run --label ... --ids ..."` launched via WMI; logs in runs/logs/.

## Next 5 steps
1. Run improver on dev-v1 evidence -> playbook gen1 (gated); rebuild proxy with new allowlist.
2. dev-v2 on 2-3 dev papers to sanity check gen1 (not reported as the result).
3. Held-out: run v1 and v2 on the same held-out papers (queue order) -> before/after numbers + trace IDs.
4. Package >=1 task, oracle-validate, run trainability filter.
5. Docs: README, unit-economics (measured), evidence.md, demo script, posts; secrets scan; push public repo.

## Open blockers
- neatlogs CLI readback needs human device approval (HUMAN_TASKS #1). SDK Doctor probe passes.
- Entire public checkpoint link needs the GitHub repo imported in Entire (HUMAN_TASKS #3).
- cfo.ai is browser-only (HUMAN_TASKS #4).
