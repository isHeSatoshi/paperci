# Demo script (3-4 min) and shot list

Record at 1080p. Use the anonymized dashboard (`docs/dashboard_public.html`) on screen; do not show verdicts
next to named papers. Numbers below come from `docs/evidence.md` (measured only).

## 0:00-0:20 Hook
- Shot: GitHub PR with a CodeRabbit-style bot comment, then an arXiv listing.
- VO: "Every pull request gets a bot that runs the tests. Research papers get nothing. Most papers' code is
  never re-run. PaperCI is CI for arXiv."

## 0:20-0:55 What it does (one paper, live or recorded)
- Shot: terminal `python -m paperci watch` -> `prepare` -> `run --variant v2 --ids <paper>`.
- Shot: neatlogs trace tree for that paper: WORKFLOW paper:* -> AGENT stage:builder -> LLM + TOOL spans ->
  GUARDRAIL verdict. Point at the tool:dry_run_offline span and the offline verifier runs.
- VO: "It watches fresh papers that ship code, gates out GPU and API-key work, seals the paper's headline
  numbers, and lets a builder agent get the code running in a locked sandbox. The builder never sees the
  numbers. An independent verifier from a different model family reruns the code with the network off and
  compares."

## 0:55-1:30 Safety and integrity (fast)
- Shot: `scripts/test_sandbox.py` output (non-root, pypi 200, example.com blocked, offline unreachable).
- Shot: report.md integrity line (hardcoded-value scan, copied-result hashes, refused hosts).
- VO: "Non-root, no capabilities, allowlist proxy during setup, no network during verification. If the
  builder types a sealed number or copies a results file, the verdict can't be 'verified'."

## 1:30-2:30 It improves itself (the core)
- Shot: dashboard "Runs" table: heldout-v1 vs heldout-v2 rows. Read the numbers: built 2/5 -> 3/5,
  avg turns 16.2 -> 10.2, LLM cost per attempt $0.255 -> $0.182, validated tasks 0 -> 1.
- VO (honesty beat): "Then I audited the wins. Two of the three v2 builds weren't faithful: one replayed a
  stored results file, one scored a formula the agent wrote. The verifier now catches both and every run was
  re-judged. Faster and cheaper, yes; faithful reproductions, still one."
- Shot: `data/playbook_history/gen1.json` and `gen2.json`: accepted vs rejected rules, the "stale" rejection,
  the controller gate.
- VO: "Generation 1 learned rules from its own failures, but traces showed advice alone did not change
  behaviour. So generation 2 escalated to an enforced policy. Every proposal passes gates: it must match real
  failures, can't advise faking numbers, can't rely on stale knowledge, and must fit the cost cap. The judge,
  the sealed claims and the held-out set are frozen, so it can't improve by grading itself easier."
- Shot: neatlogs trace pair BEFORE `294209f0b2d1b60bfc1509fdbece2f1b` (19 LLM calls, 274k tokens) vs AFTER
  `4e85d604ac7a5d343950ee923f573502` (13 LLM calls, 142k tokens), same held-out paper.

## 2:30-3:00 Failure and recovery
- Shot: chaos run trace `14ed5fe7f083b2b1ff4c98fbbf705223`: CHAOS log -> fetch fails ("Could not resolve
  proxy") -> self-heal "restarted stopped egress proxy" -> retry ok -> agent gives up honestly in 5 turns.
- Shot: a builder trace where a dry run is rejected offline and the agent fixes it (if available).

## 3:00-3:30 Output: a verified task
- Shot: `registry/tasks/<task>/` tree (instruction.md, task.toml, environment/, solution/, tests/).
- Shot: oracle reward 1.0, trainability label, spec_risk flag.
- VO: "Every successful run becomes a Harbor-format task whose oracle is validated by replay, labelled by how
  often a cheap solver can do it."

## 3:30-3:50 Sponsors + business
- Entire: checkpoint page for the gen2 commit (Sessions + Changes) - "the reasoning behind each
  self-improvement is attached to the commit".
- cfo.ai: plan screen (pricing, COGS per attempt ~$0.21, runway).
- VO: close with the honest funnel: "145 papers watched, 55 pass a CPU gate, 1 faithful held-out
  reproduction so far. The bottleneck is compute and honest verification, and the next step is GPU workers
  and simulation environments."
