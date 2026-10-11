# Build-in-public drafts (#neatHack) — for the human to post; the agent does not post

Rules for all posts: no verdicts about named papers, no keys, numbers only from docs/evidence.md.
Tag @neatlogs @entireio @cfo_ai where appropriate.

## Post 1 — the idea (Sat/Sun)
Every PR gets a bot that runs the tests. arXiv papers get nothing.

Building PaperCI for #neatHack: "CI for arXiv". It watches fresh papers that ship code, runs the code in a
locked-down sandbox, checks the headline numbers with an independent verifier, and turns successes into
verified agent tasks.

Rule #1: the agent that builds never sees the numbers it has to hit (sealed claims). #neatHack

## Post 2 — the funnel is brutal (with dashboard screenshot, anonymized)
Day 1 numbers from PaperCI: 145 fresh code papers -> 55 pass a CPU-feasibility gate.
Top reasons for rejection: large pretrained models (43), hosted-LLM API keys (29), GPU-scale (21).
Most new ML code can't be re-run on a laptop. That is the real reproducibility gap. #neatHack

## Post 3 — traces caught my bugs (neatlogs screenshot of a trace tree)
First end-to-end run failed: 18 turns, 12 of them reading notebooks, never ran anything.
The neatlogs trace also showed what I'd never have seen in logs:
- `bash -l` reset PATH, so the agent used the wrong Python
- `cmd | tail` hid failing exit codes
- the proxy refused openml.org 10 times
All three fixed from one trace. @neatlogs #neatHack

## Post 4 — it rewrites its own playbook (screenshot of gen1/gen2 history)
PaperCI now improves itself from its traces:
gen1: an improver agent mined failures -> 8 proposed rules, 4 accepted, 4 rejected by gates (2 were STALE:
they said a host was blocked that I had since allowlisted).
gen1 didn't change behaviour on dev (still ran out of turns), so gen2 escalated from advice to an enforced
policy: run code by turn 5, script by turn 8.
The judge is frozen; it can only improve the builder. #neatHack

## Post 5 — before/after on held-out (two neatlogs traces side by side)
Held-out papers (never used for tuning), same sealed claims:
v1: 2/5 built, 16.2 avg turns, $0.255/attempt, 0 validated tasks
v2: 3/5 built, 10.2 avg turns, $0.182/attempt, 1 validated task
Then I audited my own wins: 2 of the 3 v2 builds were not faithful (one replayed a stored results file,
one scored a formula the agent wrote itself). The verifier now flags both, and every run was re-judged.
Faster and cheaper: yes. "More reproductions": only 1 faithful one, same as before. Honest numbers > pretty ones.
Traces: 294209f0... vs 4e85d604... #neatHack @neatlogs

## Post 6 — Entire checkpoints + cfo.ai
Every self-improvement commit in PaperCI carries an @entireio checkpoint: the session, the reasoning,
the diff. You can see why gen2 exists, not just what changed.
And the business plan in cfo.ai: COGS ~$0.21/attempt (measured LLM + assumed CPU), $0.91 LLM per validated
held-out task (n=1, small sample), priced per verified task.
Repo: https://github.com/isHeSatoshi/paperci #neatHack

## Post 7 — chaos test (neatlogs trace screenshot)
I pulled the plug on PaperCI's network proxy mid-run on purpose. The repo fetch failed, the harness noticed,
restarted the proxy, retried, and logged the failure as recovered. Then the agent read the README, saw the
benchmark data isn't released, and gave up honestly in 5 turns for $0.09 instead of burning 18. #neatHack
