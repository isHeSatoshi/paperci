# Evidence (generated 2026-10-11 10:34 IST by scripts/build_evidence.py)

All numbers below are measured from `data/paperci.sqlite`, run artifacts and neatlogs readback.
Paper identities are anonymized (P-xxxxxx); per-paper verdicts are not published.

## 1. Before / after on the held-out split (same papers, same sealed claims)

v1 = baseline builder (playbook gen 0, base image). v2 = self-improved builder (playbook gen 2: learned
rules + enforced controller, learned image gen1). Papers taken in queue order (newest first), not picked.

| paper | v1 status | v1 turns | v1 $ | v2 status | v2 turns | v2 $ | v1 trace | v2 trace |
|---|---|---|---|---|---|---|---|---|
| P-51cee1 | max_turns | 18 | 0.3204 | max_turns | 18 | 0.2892 | `a9540916517fe38646791aacb7bade88` | `892df14168f4210e9594646da9034048` |
| P-83da0c | gave_up | 16 | 0.1924 | gave_up | 7 | 0.0899 | `e261ea87d9c32fb1968cd051a2ac9752` | `6744374a9236124e5715f1361db5f511` |
| P-d73559 | built | 18 | 0.3192 | built | 10 | 0.1445 | `294209f0b2d1b60bfc1509fdbece2f1b` | `4e85d604ac7a5d343950ee923f573502` |
| P-6a2f64 | built | 11 | 0.1692 | built | 6 | 0.0899 | `5bb421a46d58b4e2a562b80325e05d54` | `743e966776a924a24e662d288bc41105` |
| P-73e4a2 | max_turns | 18 | 0.2465 | built | 10 | 0.1233 | `553880c675b8fd62afafa4acc140bcb0` | `80481abc951ae433aa67a126d6925222` |

| metric | heldout-v1 (before) | heldout-v2 (after) |
|---|---|---|
| papers_attempted | 5 | 5 |
| built | 2 | 3 |
| build_success_rate | 0.4 | 0.6 |
| metric_measured | 2 | 3 |
| integrity_flagged | 2 | 2 |
| integrity_flags | {'no_repo_code_path': 1, 'precomputed_results_replay': 1} | {'no_repo_code_path': 1, 'precomputed_results_replay': 1} |
| faithful_measured | 1 | 1 |
| claims_within_tol_n2 | 0 | 2 |
| avg_builder_turns | 16.2 | 10.2 |
| avg_build_minutes | 6.59 | 4.96 |
| builder_cost_usd | 1.2477 | 0.7367 |
| cost_per_attempt | 0.2552 | 0.1822 |
| packaged | 0 | 1 |
| oracle_validated | 0 | 1 |
| failures_total | 11 | 7 |
| failures_recovered | 0 | 1 |
| verdict counts | {'blocked-infra': 3, 'inconclusive': 2} | {'inconclusive': 2, 'toy-scale': 1, 'blocked-infra': 2} |
| build status | {'max_turns': 2, 'built': 2, 'gave_up': 1} | {'built': 3, 'gave_up': 1, 'max_turns': 1} |

## 2. Self-improvement generations (data/playbook_history)

- **gen1**: learned from clusters {'nonzero_exit': 2, 'python_error': 2, 'egress_denied': 5, 'turn_budget_exhausted': 3, 'late_give_up': 4, 'timeout': 1}; accepted 4 rules (needs_credentials_or_gpu_give_up, too_many_read_cmds_no_submit, grep_xargs_no_match_nonzero, empty_command_timeout); rejected 4 ({'stale': 2, 'pattern matches none of its own failures': 2})
- **gen2**: learned from clusters {'nonzero_exit': 2, 'python_error': 2, 'egress_denied': 5, 'turn_budget_exhausted': 5, 'late_give_up': 4, 'timeout': 1}; accepted 3 rules (early_blocker_detection, broken_pipe_notebook_inspection, grep_xargs_no_match_nonzero); rejected 5 ({'stale': 2, 'advice violates integrity policy': 1, 'pattern matches none of its own failures': 2}); controller 18/5/8 gate=[True, 'bounds ok; projected worst-case builder cost $0.30']
- Environment learning: `huggingface_hub` promoted to the sandbox image (installed in 2 dev papers, install-gated) -> `paperci/sandbox:gen1`.

## 3. Dev-split history (used for tuning; not the reported result)

| label | attempted | built | avg turns | $/attempt | status |
|---|---|---|---|---|---|
| dev-v1 | 7 | 0 | 16.86 | 0.2253 | {'max_turns': 3, 'gave_up': 4} |
| dev-v2 | 2 | 0 | 18.0 | 0.2286 | {'max_turns': 2} |
| dev-g2 | 2 | 1 | 14.0 | 0.2342 | {'max_turns': 1, 'built': 1} |

## 4. Failure taxonomy and recoveries

Builder failure classes (all runs): turn_budget_exhausted 8, python_error 7, missing_module 6, nonzero_exit 5, egress_denied 4, data_missing 4, timeout 3, api_incompat 2, proxy_unreachable 1

Recovered failures (the agent worked around it inside a build that succeeded, or harness self-heal + retry):
- P-73e4a2 heldout-v2: python_error (trace `80481abc951ae433aa67a126d6925222`)
- P-646513 dev-chaos: nonzero_exit (trace `14ed5fe7f083b2b1ff4c98fbbf705223`)
- P-13d4e4 dev-chaos2: proxy_unreachable (trace `c07db8e5e1be1fb9872ccdf8636faf6c`)

Fault-injection (chaos) runs (`PAPERCI_CHAOS=proxy_down` stops the egress proxy before the repo fetch):
- P-646513 dev-chaos: fetch failed (`curl: (5) Could not resolve proxy: paperci-egress`), classified `nonzero_exit`, self-heal restarted the proxy, retry succeeded -> recovered=True; trace `14ed5fe7f083b2b1ff4c98fbbf705223`
- P-13d4e4 dev-chaos2: fetch failed (`curl: (5) Could not resolve proxy: paperci-egress`), classified `proxy_unreachable`, self-heal restarted the proxy, retry succeeded -> recovered=True; trace `c07db8e5e1be1fb9872ccdf8636faf6c`

## 5. Funnel and cost

- Watched (fresh, has code): 145; triage accept: 55; grounded sealed claims: 44 papers / 173 claims
- Triage reject reasons: {'large pretrained model signals': 43, 'needs hosted-LLM/API credentials': 29, 'GPU-scale signals in README': 21, 'no Python code': 20, 'repo too large': 7, 'llm': 7, 'empty repo': 4, 'repo unavailable': 3}
- Front half (snapshot+triage+claims) cost: $0.5528
- Total LLM spend: ledger $5.655 vs proxy-reported $5.655

## 6. neatlogs readback (CLI, OAuth profile; SDK key excluded from the CLI env)

- chaos `14ed5fe7f083b2b1ff4c98fbbf705223` finalization `dlq` (recovery is also recorded in the state DB `failures` table and `runs/logs/dev-chaos.log`)
- chaos `c07db8e5e1be1fb9872ccdf8636faf6c` finalization `None` (recovery is also recorded in the state DB `failures` table and `runs/logs/dev-chaos.log`)
- `a9540916517fe38646791aacb7bade88` finalized 723s, 0 LLM calls, 0 tool calls, 0 tokens, tags ['paperci', 'run:heldout-v1', 'variant:v1']
- `892df14168f4210e9594646da9034048` finalized 800s, 3 LLM calls, 2 tool calls, 38603 tokens, tags ['paperci', 'run:heldout-v2', 'variant:v2']
- `e261ea87d9c32fb1968cd051a2ac9752` None 0s, None LLM calls, None tool calls, None tokens, tags None
- `6744374a9236124e5715f1361db5f511` dlq 0s, 0 LLM calls, 0 tool calls, 0 tokens, tags ['paperci', 'run:heldout-v2', 'variant:v2']
- `294209f0b2d1b60bfc1509fdbece2f1b` finalized 1532s, 19 LLM calls, 22 tool calls, 274635 tokens, tags ['paperci', 'run:heldout-v1', 'variant:v1']
- `4e85d604ac7a5d343950ee923f573502` finalized 1550s, 13 LLM calls, 18 tool calls, 141968 tokens, tags ['paperci', 'run:heldout-v2', 'variant:v2']

Known neatlogs-side issue: trace `7bfd8f9e85e8a9e5a02622e3484c574c` (dev, 744 s) has finalizationStatus `dlq`
and spans readback returns TRACE_FINALIZATION_FAILED; a 858 s trace (`9d87b5e064829420dd778963e09904f7`) finalized fine,
so duration alone is not the cause. LLM span costUsd is 0 because the proxy model names are unpriced in
neatlogs; cost is carried as `paperci.cost_usd` attributes and LOG spans from our ledger.

## 7. Entire checkpoints

```
branch       main
  checkpoints  3

● 01M4MKYQ3938RM8FJ5K9ETQ55A  "# Mission\r\nYou are the lead engineer on \"PaperCI\" (workin..."
  10-11 10:06 (e3b748d) Self-audit and judge hardening: replay/no-repo-code-path integrity flags, re-...

● bf4a7d1a-7a3  [temporary]  "# Mission\r\nYou are the lead engineer on \"PaperCI\" (workin..."
  10-11 03:57 (abf4946) # Mission

● 01M4KP3DNR0TF8TWQE611MJJYV  "# Mission\r\nYou are the lead engineer on \"PaperCI\" (workin..."
  10-11 01:24 (e70be0f) Add verifier, robustness, Harbor packager, oracle + trainability, orchestrato...
```
