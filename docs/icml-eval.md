# External eval: PaperCI vs Hugging Face's ICML 2026 community reproductions

In July 2026, Hugging Face's ICML 2026 Open Reproductions challenge had 1,221 people use coding agents to
reproduce 2,226 ICML papers claim by claim. A GLM-5.2 judge read every logbook and froze 35,908 claim verdicts
(`ICML-2026-agent-repro/verdicts`, paper index in `ICML-2026-agent-repro/challenge`). Their verdict scale
(verified, falsified, toy, inconclusive) matches PaperCI's. Code: `paperci/icml_eval.py`.

## Reference labels

A paper is a reference only if at least two medium- or high-quality logbooks exist and at least 75% of them
agree on the paper-level outcome: **verified** (every claim verified) or **falsified** (some claim falsified).
Contested and toy-only papers are excluded for now. On the frozen data: 116 papers (47 verified, 69 falsified).

The consensus is the answer key. It comes from an LLM judge reading agent write-ups, so it can be wrong; that
is why only papers with agreement between independent teams are used.

## Protocol

1. Screen candidates in a fixed order (hash of the arXiv id): find the code, triage for the CPU sandbox, seal
   the paper's own numeric claims. Hugging Face's verdicts are never shown to any PaperCI agent.
2. The first 2 runnable verified papers become the evolution benchmark (`data/icml/bench.json`) and are
   excluded from the score; every other runnable paper is eval-only.
3. Blind runs: the full pipeline (build, offline verification, seed reruns, verdict), without packaging.
   They run as preemptible background jobs, so author submissions still go first.
4. Score: PaperCI verified -> verified, falsified-candidate -> falsified, toy-scale -> toy; anything else is
   "no decision". Reported: coverage (how often PaperCI decides), agreement when it does, and how many
   verified / falsified papers it recovers end to end (no decision counts as a miss).
5. Results are public by name at `/icml` (the papers and community verdicts are already public on Hugging
   Face), each marked as an automated result, with PaperCI's full review page and links to the logbooks.
6. Daily golden check: one paper where PaperCI and the consensus agreed is re-verified every night; a changed
   verdict is an incident on the board.

## Models

All roles run through the Factory API key (`droid exec`, locked down). The always-on roles (triage, claim
sealing, verifier, concierge) use `glm-5.3-flash`. The builder, solver, nightly improver and evolution
proposer use `deepseek-v4.1-flash` at high reasoning (`PAPERCI_MODEL_BUILDER` / `PAPERCI_MODEL_SOLVER`
override `PAPERCI_MODEL_ALL`), so builder and verifier are different model families again.

## Known biases

- The verifier is GLM-5.3-Flash and the Hugging Face judge is GLM-5.2: same model family, so agreement may be
  inflated.
- Hugging Face participants had GPUs; PaperCI is CPU-only, so it will often decline (coverage < 100%) or reach
  only toy-scale evidence on papers they could verify at scale.
- Claim sets differ: Hugging Face claims are text extracted by their indexer, PaperCI's are numeric claims it
  seals itself. Paper-level agreement is reported now; claim-level matching is future work.

## Commands (server, `~/paperci`)

    .venv/bin/python -m paperci.icml_eval fetch          # download + build reference labels
    .venv/bin/python -m paperci.icml_eval screen 60      # screen the next 60 candidates
    .venv/bin/python -m paperci.icml_eval queue 10       # queue 10 blind runs (the worker runs them)
    .venv/bin/python -m paperci.icml_eval status         # counts + score
