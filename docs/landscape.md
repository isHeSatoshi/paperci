# Landscape and novelty check (2026-10-11)

The initial brief listed four "closest matches" written by another agent. Each was checked with a web search
before being repeated here. Items marked **unverified** could not be found and are not used as claims.

| Project | What it does | Status of check | Overlap with PaperCI |
|---|---|---|---|
| PaperBench (OpenAI, ICML 2025) | Agents replicate 20 ICML 2024 papers from scratch; rubric-graded | Verified (openai.com/index/paperbench, arXiv 2504.01848) | Static benchmark, hand-built rubrics, old papers, GPU-scale. PaperCI is continuous, fresh papers, CPU-scale, numeric verifier. |
| Paper2Code (ICLR 2026) | Generates code from papers that have no code | Verified (github.com/going-doer/Paper2Code) | Generation, not verification of released code. |
| VERITAS (ChicagoHAI) | Replication agent + separate evaluation manager writes a report | Verified (github.com/ChicagoHAI/veritas) | Closest on "agent reproduces, separate judge grades". On-demand, not a watcher; does not package executable tasks. |
| AutoSciBench (arXiv 2610.05140, 2026-10-04) | Autonomous generation and revision of scientific benchmarks from solver trajectories and judge feedback | Verified | Closest on "benchmarks that improve themselves". Concept-driven tasks, not tied to each new paper's released code. |
| GitTaskBench / SUPER / CORE-Bench | Agents set up and run research repos | Verified (arXiv 2508.18993 and AstaBench report) | Fixed task sets curated by humans. |
| "Paper2Bench" (paper -> benchmark task families) | as described in the brief | **Unverified**: no repository or PyPI package found | not used |
| "arXiv-to-code" GitHub org (daily generated implementations) | as described in the brief | **Unverified**: only daily arXiv crawlers/summarizers found | not used |

## What appears to be new in PaperCI

Each piece exists somewhere. What we did not find is the combination, running continuously:

1. **Triggered by publication, like a PR bot.** CodeRabbit-style "CI for arXiv": every fresh code paper
   gets an automatic attempt, with no human curation.
2. **Sealed claims plus an independent verifier.** The agent that builds never sees the numbers it must
   hit. The judge is a different model family, runs offline from artifacts only, and is never modified by
   self-improvement.
3. **Two outputs from one run:** a reproducibility report, and a Harbor-format executable task whose
   oracle is validated by replay and whose difficulty is labeled by a cheap solver (trainability filter).
4. **The system improves with volume.** Traced failures become playbook rules. Recurring dependencies are
   promoted into the base image. Denied hosts become allowlist proposals that a human must approve.
   More papers make cheaper, more successful builds.

## Why "contamination-free" holds only partly

Tasks are built from papers newer than most model training cutoffs. The paper text and README are still
public, so a solver could read the numbers. The hidden verifier compares against our own offline
measurement (at the scale we ran), not the paper's printed value. This helps at toy scale but is not a
guarantee. See README "Limits".
