# Demo script (3:30) and shot list: "CodeRabbit for research papers"

Record at 1080p. On screen, use only anonymized pages (`--anonymize` reviews, `registry/reviews/index-public.html`,
`docs/dashboard_public.html`, `docs/img/*`): no verdict next to a named paper. Numbers are from
`docs/evidence.md` (measured only).

Prep (before recording): pick a fresh dev paper with `python scripts\show_queue.py dev review 5`, then
`python -m paperci review <id> --anonymize --open` (~12 min, ~$0.15). Record the terminal while it runs and
speed it up 8x in editing. Keep `docs/img/terminal.png` as a fallback.

## 0:00-0:20 Hook
- Shot: a GitHub PR with a CodeRabbit comment. Cut to an arXiv listing.
- VO: "Every pull request gets a bot that runs the tests and reviews the change. Research papers get nothing.
  Most papers' code is never re-run by anyone. PaperCI is CodeRabbit for papers."

## 0:20-1:05 One command, live (terminal, sped up)
- Shot: `python -m paperci review <arxiv-id>`; the CI-style stream: triage, claims sealed (sha256), builder turns,
  the **learned controller firing** ("stop reading, run code"), the offline dry run, three reruns with
  `--network none`, the oracle replay.
- VO: "It doesn't lint the code. It reads the paper, seals every headline number so the agent never sees
  them, rebuilds the environment from scratch in a locked sandbox, and gets the code running. Then a
  different model family reruns it three times with the network off and checks each claim."

## 1:05-1:50 The review (browser, anonymized review page)
- Shot: scroll the review: pipeline strip, walkthrough, **claims vs measured** (paper value, measured mean ± sd,
  tolerance, status), checks, **suggestions for the authors**, "reproduce it yourself", the badge.
- VO: "This is the PR comment for a paper: claim by claim, what the paper says and what the code actually
  produced. Honest labels: reproduced, reproduced at reduced scale, differs, not measured. And concrete fixes
  for the authors: missing dependencies, data that isn't released, numbers no script prints."

## 1:50-2:20 It catches cheating, including its own agent's
- Shot: `docs/img/review-caught.png`: red "Computed, not replayed" check, every claim "Run not trusted".
- VO: "When I audited my own wins, the agent had found shortcuts: replaying stored results, or computing the
  metric with its own formula instead of the paper's code. The judge now catches both, and every run was
  re-judged. A review you can't trust is worse than none."

## 2:20-2:55 It improves itself (held-out numbers)
- Shot: `docs/evidence.md` section 1 table (or dashboard Runs rows). Read: on 10 held-out papers it never tuned on,
  built 3 -> 5, faithful reproductions 1 -> 2, validated tasks 0 -> 2, turns 16.5 -> 11.8, cost per paper
  $0.24 -> $0.18.
- Shot: neatlogs trace pair, same paper: BEFORE `294209f0b2d1b60bfc1509fdbece2f1b` (19 LLM calls, 274k tokens)
  vs AFTER `4e85d604ac7a5d343950ee923f573502` (13 LLM calls, 142k tokens).
- VO: "It learns from its own neatlogs traces: rules, an enforced control policy, a better base image. Every
  change is gated; the judge, the sealed claims and the held-out papers are frozen."

## 2:55-3:10 It heals itself
- Shot: chaos run: proxy killed -> "Could not resolve proxy" -> self-heal restarts it -> retry ok
  (`runs/logs/dev-chaos2.log` or the evidence section 4 lines).
- VO: "Kill its network mid-run and it repairs itself and keeps going."

## 3:10-3:30 Why it matters + sponsors
- Shot: `registry/reviews/index-public.html` (PR-list of reviews) and a Harbor task folder.
- VO: "Every reproduction becomes a verified, executable task, so fresh papers become fresh benchmarks. Today
  that's deterministic code on a CPU; next is GPUs and simulators: robotics, physics, chemistry, where
  verifying results is the bottleneck. Built with neatlogs tracing every call, Entire checkpoints on every
  commit, and a cfo.ai plan at about 20 cents per attempt."
