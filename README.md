# PaperCI: continuous integration for arXiv

PaperCI is a CodeRabbit-style bot for research papers. It watches fresh arXiv papers that ship code, tries
to run that code inside a locked-down sandbox, checks the paper's headline numbers with an independent
verifier, and turns each verified run into an executable task (Harbor format) for evaluating or training
agents. It also improves itself: failures it sees in its own traces become new recovery rules, a learned
control policy, and a better sandbox image. Promotion is gated, so it can never improve by moving its own judge.

Built for neatHack 2026 with **neatlogs** (every LLM call and stage traced), **Entire** (build sessions
captured as checkpoints on commits) and **cfo.ai** (business plan, see `docs/unit-economics.md`).

## What happens to one paper

```
watcher (arXiv API + HF Daily Papers) ─> triage gate (rules + nova-micro)
  ─> sealed claim extraction (nova-pro, grounded quotes, sha256-sealed)
  ─> builder agent (claude-sonnet-4.6, own tool loop, sandbox tools; never sees claim values)
       └─ offline dry run on submit (network disconnected)
  ─> independent verifier (fresh container, --network none, reads artifacts only;
       deterministic extraction first, nova-pro only for ambiguous logs, quote must exist)
  ─> robustness (re-runs with other seeds, tolerance calibration)
  ─> verdict: verified | falsified-candidate | toy-scale | inconclusive | blocked-infra
  ─> packager (Harbor task: instruction.md, task.toml, environment/Dockerfile, solution/, tests/)
  ─> oracle check (replay builder's steps in a fresh env, hidden test must give reward 1)
  ─> trainability filter (claude-haiku solver, k attempts; keep "solved sometimes")
  ─> registry + dashboard (funnel, cost per stage, cost per validated task)
```

Self-improvement loop (`python -m paperci improve`, `python -m paperci envlearn`):

```
neatlogs-traced runs ─> failure taxonomy rebuilt from raw sandbox logs
  ─> improver agent proposes playbook rules + controller knobs
  ─> deterministic gates (regex recall/precision on real failures, integrity ban-list,
     stale-knowledge check vs current allowlist, cost projection vs cap)
  ─> new playbook generation (data/playbook.json, history in data/playbook_history/)
  ─> envlearn: packages installed in >= 2 papers are promoted into the sandbox image (install-gated)
  ─> measured on the held-out split, which the improver never sees
```

Frozen by design (the improver cannot touch these): verifier, claim sealing, tolerances, held-out split.

## Safety and integrity
- Third-party code only runs in Docker: uid 1000, `--cap-drop ALL`, `no-new-privileges`, CPU/mem/pid limits,
  no host mounts, no credentials. Build phase: `--internal` network whose only exit is a tinyproxy
  allowlist (PyPI, PyTorch wheels, GitHub, Hugging Face, Zenodo, UCI, OpenML). Verification: `--network none`.
- Repos are fetched as tarballs inside the sandbox; artifacts come out as tar streams, extracted file by file
  with path checks.
- Claims are sealed (sha256) before any build; the builder sees metric names only, README numbers matching
  sealed values are redacted, unseal is permission-checked and logged.
- Anti-cheat: sealed values searched in everything the builder authored (script, writes, commands, repo diff);
  output files hashed against the pristine repo (copied results); proxy logs list refused hosts.
- Integrity checks added after auditing our own held-out results (each one caught a real case):
  `precomputed_results_replay` (the run script loads stored results / finishes in seconds while reading
  result-like artifacts), `no_repo_code_path` (the run script never touches a repo module, e.g. a formula the
  builder wrote itself), claim-id tagged evidence for claims sharing one metric name, header-aware extraction
  (numbers in a `--- c2: ... 6000 steps ---` header are settings, not results), and whitespace-tolerant
  extraction specs (multi-line evidence). Any integrity flag blocks packaging; `scripts/reverify.py` re-judges
  every stored run with the current rules.
- A packaged task whose own oracle does not earn reward 1 is moved to `registry/rejected/`. Container deaths
  (daemon crash, exit 135/255) are retried as infra failures, never counted as task failures.
- Repo text is treated as untrusted (prompt-injection warnings in every system prompt).
- Verdicts about named papers are not published. `runs/`, `registry/` and reports are git-ignored; the public
  dashboard is anonymized.

## Setup (Windows, PowerShell; Linux/macOS analogous)
1. Python 3.12 venv: `uv venv .venv; uv pip install --python .venv\Scripts\python.exe "neatlogs[openai]" openai pydantic requests python-dotenv`
2. `.env` (git-ignored) with `NEATLOGS_API_KEY` (project key), `LITELLM_API_KEY`, `LITELLM_BASE_URL`, optional `CRUSTDATA_API_KEY`.
3. Docker Desktop running. Build images: `powershell -File scripts\build_images.ps1 -Tag base`
4. Self-test isolation: `python scripts\test_sandbox.py` (checks non-root, allowlist, offline mode, no secrets).
5. Offline unit checks: `python tests\test_offline.py`.

## Run
```
python -m paperci watch                                   # fetch fresh code papers
python -m paperci prepare --limit 150                     # snapshot + triage + sealed claims
python scripts\show_queue.py heldout heldout-v1 5         # next papers in queue order
python -m paperci run --label heldout-v1 --variant v1 --ids <ids...>   # baseline builder
python -m paperci improve --labels dev-v1 --apply         # learn playbook generation N+1 from dev traces
python -m paperci envlearn --apply                        # promote recurring packages into the image
python -m paperci run --label heldout-v2 --variant v2 --ids <same ids>  # improved builder
python -m paperci stats | budget | dashboard [--public]
python scripts\nl_trace.py summary <trace-id>             # read the trace back from neatlogs (CLI)
```
Budgets (env-overridable, see `paperci/config.py`): $16 hard stop, $3 demo reserve, $0.60 per paper,
$0.45 builder, 18 turns default; every LLM call is pre-checked against the ledger and reconciled with the
proxy's `/key/info` spend.

## Limits (honest)
- CPU-only, minutes-scale. Most fresh ML papers need GPUs, hosted LLM APIs, gated data, or large weights;
  they are rejected at triage or end as `blocked-infra`. The funnel shows this; it is the main limitation.
- Claims come from text (arXiv HTML / HF markdown). Strict grounding drops table numbers whose quotes do not
  survive HTML stripping.
- Reduced-scale runs are `toy-scale` and never count as verified; packaged tasks then reference our own
  offline measurement, not the paper's number.
- "Contamination-free" is partial: papers and READMEs are public. The hidden verifier uses our measurement.
- Trainability uses k=2 cheap-solver attempts: a coarse signal.
- Long traces (>10 min) were once observed to fail neatlogs finalization (DLQ); see docs/evidence.md.

## Repo map
`paperci/` pipeline (stages in `paperci/stages/`), `sandbox/` images, `scripts/` ops helpers, `tests/`,
`docs/` (landscape, unit economics, evidence, demo script, posts), `data/playbook.json` (learned policy).
