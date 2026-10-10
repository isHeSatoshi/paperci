# neatHack build log: PaperCI

## Project
- **Name:** PaperCI (working name)
- **One sentence:** A CodeRabbit-style bot for arXiv. It watches fresh papers that ship code, tries to run
  that code in a locked-down sandbox, checks the headline numbers with an independent verifier, and
  turns each success into a verified, executable task for evaluating or training agents.
- **Problem:** Most papers' code is never re-run by anyone. Reproduction costs a human days, and agent
  benchmarks go stale and leak into training data. PaperCI makes reproduction a continuous,
  cheap, auditable signal, and turns fresh papers into fresh, contamination-resistant tasks.

## Team
- (fill in the names/handles you want public)

## Stack (actually used)
- Python 3.12, own tool-calling loop (no agent framework), Pydantic typed outputs, SQLite state.
- LLMs via an OpenAI-compatible LiteLLM proxy: Claude Sonnet 4.6 (builder + improver), Claude Haiku 4.5
  (cheap solver), Amazon Nova Pro (claims + verifier, a different family than the builder), Nova Micro (triage).
- **neatlogs** Python SDK 1.4.26: `neatlogs.wrap(OpenAI(...))` for every LLM call; one WORKFLOW trace per
  paper; CHAIN/AGENT/TOOL/GUARDRAIL spans per stage; cost and failure class stamped on spans.
- **Entire** CLI 0.11.4 enabled for Factory Droid; build sessions attached as checkpoints on commits.
- **cfo.ai** for the business plan (human step, see HUMAN_TASKS.md; inputs in docs/unit-economics.md).
- Docker Desktop: hardened sandbox image + tinyproxy egress allowlist on an `--internal` network.
- Sources: arXiv API + arXiv HTML, Hugging Face Daily Papers API (+ HF paper markdown), GitHub REST API.

## Decisions (one line each)
- 2026-10-11 Own Python loop instead of a framework so every model/tool call is a neatlogs span.
- 2026-10-11 Sandboxes: non-root uid 1000, `--cap-drop ALL`, no-new-privileges, 4 CPU / 6 GB / 1024 pids, no host mounts, no secrets.
- 2026-10-11 Egress: build phase only via allowlist proxy; verification runs with `--network none`.
- 2026-10-11 Repo code is fetched as a tarball *inside* the sandbox; nothing from a repo runs or is extracted on the host.
- 2026-10-11 Artifacts copied out as a tar stream and extracted file-by-file with path checks (no tar-slip).
- 2026-10-11 Prices taken from LiteLLM `/v1/model/info`; ledger also reconciles with `/key/info` spend (authoritative).
- 2026-10-11 Budget: hard stop $16 of the $20 key, $3 demo reserve, $0.60 per paper, $0.45 builder, $0.12 per solver attempt.
- 2026-10-11 Claude prompt caching on the builder loop (measured: 178k of 196k prompt tokens were cache reads).
- 2026-10-11 Dev/held-out split by sha256(arxiv_id) mod 3 (1/3 held out), fixed before any builder run.
- 2026-10-11 Triage/claims are shared across run labels so before/after runs face identical sealed targets.
- 2026-10-11 Claims must be grounded: the verbatim quote has to occur in the paper text and contain the value; ungrounded claims dropped (276 -> fewer), even at the cost of losing table numbers.
- 2026-10-11 Hugging Face Daily Papers added as a second watcher feed (repo links), author fields never stored.
- 2026-10-11 Verdicts about named papers are NOT published: run artifacts, registry and per-paper reports stay local (git-ignored); the public dashboard is anonymized.
- 2026-10-11 Self-improvement may only change the builder's playbook and base image; verifier, sealing, tolerances and held-out split are frozen.
- 2026-10-11 Allowlist widened with `api.github.com` (GitHub) and OpenML (dataset host) after both were denied in real dev runs; applied equally to v1 and v2.
- 2026-10-11 Competitor list fact-checked (docs/landscape.md); two items from the brief could not be verified and are not claimed.

## Build log
### 2026-10-11 (Sun) early hours IST
- Read hackathon page, Luma, build-log skill, neatlogs Python SDK docs + setup guide, Entire hackathon page, Harbor task docs.
- Created `.gitignore` first (setup/, .env, .venv, runs, sqlite), then `git init`, `entire enable --agent factoryai-droid`.
- `.env` built from setup/ files without echoing values. LiteLLM `/key/info`: budget $20, spend $0.00 at start, 60 RPM.
- neatlogs `doctor --local`: pass. `doctor --probe`: all 8 checks pass (TRACE_VISIBLE, TRACE_FINALIZED, HIERARCHY_VALID, ...).
- Docker Desktop kept dying when started from the agent shell (child process killed on exit); fixed by launching it detached via WMI.
- Sandbox self-test (scripts/test_sandbox.py), measured: uid 1000; pypi.org 200; example.com blocked; direct bypass blocked; no host secrets in env; `--network none` unreachable; proxy log attributes egress per container IP.
- Watcher: 145 fresh papers with a GitHub repo (99 dev / 46 held-out) from arXiv API + HF Daily Papers.
- Triage v1 (nova-micro only) accepted LLM-agent benchmarks needing API keys and 3B diffusion models, and estimated "10-12 min" for almost everything. Fix: deterministic rules on README + dependency manifests (API-key/hosted-LLM signals, model-size signals). Re-triage: 55/145 accepted. Reject reasons: large pretrained model 43, hosted-LLM credentials 29, GPU-scale 21, no Python 20, too large 7.
- Front half (snapshot + triage + sealed claims) for 145 papers cost **$0.553**.
- First end-to-end run (dev, builder v1) on a conformal-prediction library paper: max turns (18) without submit; 12 of 18 turns spent reading notebooks; proxy refused `www.openml.org` 10x; cost $0.195; verdict blocked-infra. Trace `8317eed8f23841370ecf981472520ffa`.
- Bugs found from that trace: (1) `bash -lc` login shell reset PATH, so commands used system Python instead of /opt/venv (fixed: plain `bash -c` + explicit PATH); (2) `cmd | tail` hides failures (exit 0) so egress denials were not classified (fixed: masked-failure detection); (3) classifier false positive on `cat` of source code mentioning FileNotFoundError (fixed: read-only commands never classified).
- Entire: hooks were installed mid-session, so no automatic checkpoint; attached the session manually. Transcript secret-scanned first (0 hits for all 4 secrets). Checkpoint **01M4KP3DNR0TF8TWQE611MJJYV** on commit `e70be0f`.
- Dev baseline (builder v1), first 6 dev papers in queue order: see "Results" (filled from `python -m paperci stats`).

## Results (measured; numbers only from `python -m paperci stats`)
- (filled in as runs complete; see docs/evidence.md)

## Demo
- Local: `python -m paperci dashboard` -> registry/index.html (private), `--public` -> docs/dashboard_public.html.
- Script: docs/demo-script.md.

## Known limitations and next steps
- CPU-only, minutes-scale; most fresh ML papers are GPU/LLM-scale and are rejected or blocked (honest funnel).
- The verifier extracts numbers from logs; ambiguous logs fall back to an LLM extraction that must quote a line that exists.
- Toy-scale runs are labeled toy-scale; they never count as verified.
- "Contamination-free" is partial: paper and README are public. The hidden verifier uses our own offline measurement.

## Submission (confirmed requirements, from neatlogs.com/hackathon)
- Use neatlogs, Entire and cfo.ai; submit on X by quoting the official neatlogs post (team, name, project, public repo, demo), Mon 12 Oct 7:00-11:55 PM IST.
- Evidence: before/after traces with numbers; failure + recovery run; Entire checkpoint link + ID; cfo.ai plan; public repo with hackathon-dated commits; #neatHack posts.
- Outstanding (human): see HUMAN_TASKS.md.
