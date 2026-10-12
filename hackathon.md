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
  paper; CHAIN/AGENT/TOOL/GUARDRAIL spans per stage; cost and failure class stamped on spans. Prompt
  management: every distinct builder system prompt (playbook generation x controller) is a versioned
  `paperci-builder-system` prompt labelled `gen-N`/variant, and builder traces link to that version, so
  cost/turns/outcome compare across what the agent learned. Sessions: traces carry `session_id` (one web job,
  autopilot day or eval batch) and an anonymous `end_user_id` (hashed author email, `autopilot`). SDK delivery
  diagnostics are surfaced in `/healthz` and logged as an incident when a run drops spans.
- **Entire** CLI 0.11.4 enabled for Factory Droid; build sessions attached as checkpoints on commits.
  `commit_linking: always` (the default `prompt` silently skipped linking for non-interactive commits),
  PII email redaction on, and a local-only rule redacting invite codes before transcripts reach the public repo.
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
- 2026-10-11 Gen1 advice did not change behaviour on dev, so gen2 escalated to an enforced controller (run code by turn 5, script by turn 8, single target), gated by a worst-case cost projection.
- 2026-10-11 Audited our own held-out "successes" by hand before reporting them; 2 of 3 v2 builds were not faithful (replayed a stored results pickle; scored a formula the builder wrote instead of repo code). Added `precomputed_results_replay` and `no_repo_code_path` integrity flags and re-judged every run with `scripts/reverify.py`. Reported numbers use the stricter judge.
- 2026-10-11 A task whose own oracle fails is not a task: moved to registry/rejected. Container deaths are infra retries, not task failures.
- 2026-10-11 Extraction specs made whitespace-tolerant and header-aware after the 09280-class oracle failure (evidence spanned two log lines; a `--- c2: ... (slot 1), 6000 steps ---` header was read as the value 1).
- 2026-10-11 Repo stays private until a final secret scan; then made public (user authorized).

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
- Dev baseline (builder v1), 7 dev papers in queue order: 0 built (3 max_turns, 4 gave up), $0.225/attempt.

### 2026-10-11 (Sun) morning IST
- Improver gen1 (from dev-v1 traces): 4 rules accepted, 4 rejected (2 "stale": claimed hosts were blocked that had since been allowlisted). envlearn promoted `huggingface_hub` -> image gen1. dev-v2: still max_turns, so advice alone was not enough.
- Gen2: enforced controller + 3 rules (5 rejected, one for advising a "guess" of numbers). dev-g2: one paper built in 10 turns (toy-scale), oracle 1.0.
- Held-out v1 vs v2 on the same 5 papers (queue order): see Results.
- Self-audit found replay/formula cheats in 2 v2 builds; new integrity checks + re-verification (see Decisions).
- Oracle for the faithful held-out build failed (reward 0): diagnosed as an extraction-spec bug (multi-line evidence, header numbers). Fixed, re-packaged: oracle 1.0.
- Host disk C: filled up (Docker image of one dev build was 17 GB) and Docker Desktop died mid-oracle (exit 135). User freed space; we deleted our stale build images; added dead-container cleanup + low-disk warning to `ensure_infra()` and treat exit 135/255 as infra retries.
- Chaos run (`PAPERCI_CHAOS=proxy_down`, dev paper): repo fetch failed (`Could not resolve proxy`), self-heal restarted the proxy, retry succeeded, failure marked recovered; builder then gave up correctly in 5 turns ($0.088, data not released). Trace `14ed5fe7f083b2b1ff4c98fbbf705223`. The failure was classified generic `nonzero_exit`; added a `proxy_unreachable` class afterwards.

- User linked the repo in Entire. Second chaos run on another dev paper: recovered again and was now classified
  `proxy_unreachable` (the classifier fix from the first chaos run, validated). neatlogs: the CLI login is
  read-only (tag/comment FORBIDDEN, sharing read-only), and ingestion is degraded (Doctor probe now fails with
  TRACE_READBACK_TIMEOUT; 16 traces in `dlq`). Wrote `docs/neatlogs-feedback.md` for the neatlogs team.

- Product: `python -m paperci review <arxiv-id>` runs the whole pipeline with CI-style live output and ends in a
  CodeRabbit-style review (HTML + Markdown + README badge): pipeline strip, walkthrough, claims-vs-measured table,
  checks, suggestions for authors, reproduce-it-yourself. First live run on a fresh dev paper: 11.9 min, $0.149.
  Built images are now deleted after each run (one was 17 GB), and a run stops if the host disk is under 5 GB.
- Second held-out batch (5 more papers, v1 and v2) to double the before/after sample.
- Web app (`python -m paperci.web`): authors submit email + arXiv link or a paper file in any common academic
  format (PDF, LaTeX .tex / source .zip / .tar.gz, DOCX, HTML, Markdown, text) + repo. A conversational agent
  (claude-haiku) reads the paper and repo, shows the sealed claims and its plan, asks only plan-changing
  questions, turns the answer into builder guidance (sealed values redacted), narrates the run, asks again when
  blocked (data/credentials/entry point), retries up to 3 attempts, writes a summary, and can watch the repo to
  re-verify on new commits. Emails go to an outbox (no mail provider connected).
- Tested end to end in the browser: (1) arXiv paper: intake -> author reply -> attempt 1 stuck -> agent asked for the
  entry point -> attempt 2 -> agent found the data is only on a university drive -> author asked for the write-up ->
  summary -> watch started. (2) LaTeX source .tar.gz upload: claims extracted from LaTeX, intake, run, agent
  concluded the package needs CUDA (nvcc) and explained the CPU fallback fix.
- Bugs found by these runs and fixed: pip's "Cannot connect to proxy ... 403" was classified as proxy outage
  instead of a refused host; PyTorch's new CDN host `download-r2.pytorch.org` added to the allowlist; the author
  reply LLM copied a claim value into the builder notes (the builder's redaction caught it, as designed).

### 2026-10-11 (Sun) evening IST: the agent moves to the cloud and runs itself
- The hackathon gave $25 of AWS credits. Dry runs mapped the shared account's limits: us-east-1 only, up to t3.large,
  50 GB disk, no CloudFormation, SSM, SES or Bedrock. An encrypted launch was killed by
  `Client.InvalidKMSKey.InvalidState` (the account's default EBS key is broken), and the Elastic IP limit was
  already reached. Deployed one t3.large (`i-0184830ca8b082fb9`) with an idempotent boto3 script, a bootstrap
  script, systemd units and Caddy HTTPS on an sslip.io hostname. It powers itself off on Fri 2026-10-16 23:59 IST.
- Autopilot: every 90 minutes (when no author is waiting) it watches arXiv, triages and seals new papers,
  reproduces the freshest never-attempted one, and publishes an anonymized review to the live board.
- Nightly: self-improvement from the last two days of dev-split failures (held-out never feeds the learner),
  a chaos drill (kill the egress proxy, or leave an orphaned sandbox behind), and a daily digest.
- Watchdog (every 3 min, root): Docker, egress proxy and network, orphaned sandboxes, disk pressure, web health.
  Repairs are logged as incidents on the board.
- Reviews gained **verified suggested changes** like a CodeRabbit PR review: the repo patch the builder needed,
  a `reproduce.sh` entry point, and a pinned lockfile of the environment that worked. Each is verified by the
  offline reruns and downloadable as `paperci.patch`.
- Public safety: submissions need an invite code and are rate-limited per IP; a daily LLM cap (`PAPERCI_DAILY_CAP`)
  paces the autopilot on top of the per-paper, per-stage and global stops (author submissions skip the daily
  pacing but not the web spend cap or the hard stop). The board only shows anonymized ids.

### 2026-10-11 (Sun) night IST: self-improvement that is tested before it is trusted
- Honest gap found: the nightly learner adopted playbook rules on static checks alone and never tested whether
  the agent got better. Replaced it with an evolution loop (`docs/evolution.md`): candidates are a new playbook
  and a rewritten builder control policy (`paperci/stages/builder_policy.py`, the one code file the agent may
  edit, loaded through an AST-checked sandbox with no access to sealed values). Each candidate and the
  incumbent run for real on a fixed dev benchmark; adoption needs a fitness margin, no regression and no
  integrity flag. Code changes wait for a human approval; adopted changes are re-checked on live papers and
  rolled back if worse.
- Recursive part (depth 1): the improver's own strategy is chosen by UCB over the measured gains of its past
  proposals, and a fixed meta prompt writes new strategies from that scoreboard.
- The referee (verifier, sealing, tolerances, ledger, gate, this loop) is hash-pinned per round, together with
  a hash over all sealed claims; a change voids the round. Budget $1.50 per round, one round per night.

### 2026-10-11 (Sun) late night IST: an external eval from real papers, and one model for everything
- External eval against Hugging Face's ICML 2026 Open Reproductions (`docs/icml-eval.md`): 116 papers where
  independent community teams agreed (47 verified, 69 falsified) become blind reference cases; PaperCI never
  sees their verdicts. Two runnable verified papers replace the old 2-paper evolution benchmark; a daily golden
  re-check catches regressions.
- Changed course on the operator's call: no human review queue; the community consensus is the answer key, and
  results are public by paper name at `/icml` (the papers and verdicts are already public on Hugging Face),
  each marked as automated. Builder, solver, improver and evolution proposer move to DeepSeek V4.1 Flash (high
  reasoning); always-on roles and the verifier stay on GLM-5.3-Flash, which restores the family split.
- All roles moved to GLM-5.3-Flash through the Factory API key (no chat-completions endpoint exists, so each
  call is one locked-down `droid exec`: empty working directory, only TodoWrite, tool calls returned as JSON).
  Trade-offs, stated on purpose: the builder/verifier model-family split is off, and the Hugging Face judge is
  also a GLM model, so agreement may be inflated; earlier Claude-based results are not comparable.

### 2026-10-12 (Mon) 01:00 IST: learning from community logbooks, an improvement push overnight
- Logbook learning (`paperci/logbook_learn.py`): the improver reads methods notes from high-quality community
  logbooks of papers outside the ICML candidate set (never bench or eval) next to PaperCI's own failure
  clusters, and proposes general rules. Leak gate + the usual static gates, then it competes as a third
  candidate in the evolution round; at most one playbook is adopted per round. First dry run: 4 rules from 8
  logbooks passed every gate.
- Evolution now learns from dev and benchmark failures (never held-out or ICML eval runs). Fast mode until
  18:00 IST (`PAPERCI_EVOLVE_FAST_UNTIL`): rounds follow each other after 30 min instead of once per night;
  afterwards it returns to nightly by itself. Evolution and ICML eval steps alternate so neither starves.
- A restart (deploy or crash) now releases interrupted background runs, so they are redone without losing an
  attempt. GitHub token added on the server (read-only, public repos): 5,000 requests/hour instead of 60.
- README rewritten in plain language with two memes; new TECHNICAL_WRITEUP.md.

### 2026-10-12 (Mon) 10:30 IST: neatlogs as PaperCI's eyes
- Traces were hard to read: no input/output set, so neatlogs showed the first log line ("prompt registration
  skipped") as the trace input, and failed runs were OK spans, so "Execution Failed" never fired. Now every
  workflow and stage span has `input.value`/`output.value` (paper in, verdict out), failed runs (turn limit,
  sandbox error, integrity flag, failed offline run) are ERROR, and Factory LLM calls carry token counts.
- Prompt registration had failed on every run: neatlogs allows one label per prompt version. Fixed (variant
  is the label, playbook generation a tag).
- Six PaperCI regex detections (budget run-out, integrity flag, refused host, rejected dry run, honest
  give-up, claims verified).
- New `paperci/nl_api.py`: a `paperci-server` service account (one project, 7-day token) publishes every
  unrecovered builder failure category as a native neatlogs incident linked to its traces, after each job.
  First publish: 11 incidents. Trace feedback and comments need a user login, so the server does not post them.

### 2026-10-12 (Mon) 12:30-14:00 IST: what neatlogs found, and the judges page
- Triaged every Execution Failed hit, detection hit and incident since the tracing fix (5 traces, 12 clusters).
  Three were PaperCI bugs and are fixed: (1) `ValueError: No closing quotation`, the pip bookkeeping crashed 4
  builds across 3 evolution rounds on an unbalanced quote, so candidates were scored on our bug; any harness error
  in one tool call now costs a turn, not the build. (2) A verifier false positive: drivers written with a shell
  heredoc were invisible to the repo-code-path check; re-checking all 18 flagged runs cleared 2, one of which had
  tripped a playbook candidate in round 0437. (3) Google Drive was not allowlisted, so a bench paper's data could
  never download. Two flags were correct (an agent simulated data; an R-only repo), one is agent behaviour for
  evolution. Incidents for fixed patterns now close with the fix commit named.
- Night watch also found: every deploy reset the learned playbook to the committed generation (so ICML pass 2
  ran without the adopted playbooks), and the orphan sweep missed sandboxes of killed jobs. Both fixed.
- ICML pass 2 finished: flat against pass 1 (built 11 vs 11, reduced-scale claims 8/16 vs 8/22, no full-scale
  verdicts), explained by the playbook reset above.
- `/judges`: PaperCI run on its own submission; each official evidence row is a claim re-checked live (git,
  neatlogs API, run DB), with a verify command and what it does not show, plus the neatlogs triage table with
  live "since the fix" counts, a mirrored trace and `judges.json`. Demo script: `docs/demo-script.md`.
- Opened to everyone (operator's call): invite codes removed. Still bounded: 3 submissions per network per hour,
  $0.60 per paper, and spend ceilings resized for the Factory key (the old $16 hard stop was sized for the $20
  LiteLLM key and was about to stop the agent): hard stop $60, web cap $55. Raised again once the operator
  confirmed the Factory plan has no practical limit: hard stop $300, web cap $280, autopilot pacing $40/day
  (kept as runaway guards, not budgets). The bottleneck is now the 2-vCPU box, one sandbox job at a time.

## Results (measured; from `docs/evidence.md`, `docs/unit-economics.md`)
- Funnel: 145 fresh code papers -> 55 pass triage -> 44 with grounded sealed claims (173 claims). Front half $0.553.
- Held-out before/after, all 10 held-out papers with sealed claims, queue order (v1 baseline -> v2 self-improved).
  First 5 papers ran before the self-audit; the second 5 ran afterwards with the stricter judge; all 20 runs are
  judged by the same final rules:
  - built 3/10 -> 5/10; avg builder turns 16.5 -> 11.8; LLM $/attempt 0.237 -> 0.178; builder failures 29 -> 11.
  - Faithful measured runs (built, no integrity flag) 1 -> 2; claims within calibrated tolerance (n>=2 reruns) 0 -> 2;
    oracle-validated tasks 0 -> 2. Integrity flags caught 3 runs in each arm (replayed results, or a run script that
    never calls the repo's code); those are excluded from every "faithful" number.
  - First 5 only (before the audit): built 2/5 -> 3/5, faithful 1 -> 1, validated 0 -> 1.
  - Best before/after trace pair (same paper): v1 `294209f0b2d1b60bfc1509fdbece2f1b` (19 LLM calls, 274k tokens) vs v2 `4e85d604ac7a5d343950ee923f573502` (13 LLM calls, 142k tokens).
- Registry: 4 oracle-validated tasks (2 held-out, 2 dev; all reduced-scale), all "too hard for cheap solver" (0/2 Haiku). 4 packages rejected (3 integrity, 1 oracle).
- Total LLM spend: $7.77 of the $20 key (ledger = proxy-reported).

## Entire checkpoints (session reasoning attached to commits)
- **01M4KP3DNR0TF8TWQE611MJJYV** on `e70be0f` (verifier, packager, oracle, self-improvement loop):
  https://entire.io/gh/isHeSatoshi/paperci/commit/e70be0fdb9cc189abc7343dfe7f934f97d53f226
- **01M4MKYQ3938RM8FJ5K9ETQ55A** on `e3b748d` (self-audit, judge hardening, re-verification, chaos recovery):
  https://entire.io/gh/isHeSatoshi/paperci/commit/e3b748dd2178ffbdeb24eca5e159c7fb0b2b5ae1
- Transcripts were secret-scanned before attaching (0 hits for all keys and the GitHub token).

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
