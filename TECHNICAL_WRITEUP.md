# Technical write-up

## Goal

My goal was to build CI for research papers. A pull request gets a bot that runs the tests; a paper gets
nothing, and "code available" rarely means anyone has run it. PaperCI takes a paper and its repository,
rebuilds the environment from scratch, reruns the code, and checks each headline number in the paper against
what the code actually prints.

The hard part is not running code. It is making the result trustworthy. An agent that is asked to reproduce
a number will find a way to print that number. So most of the design is about who is allowed to see what,
and who grades whom.

## Approach

I started from the failure mode I was most worried about: an agent that "reproduces" a paper by copying the
answer. That gave three rules that everything else follows.

1. The agent that builds never sees the target numbers.
2. A different model, in a different container, with the network off, does the grading.
3. Every run leaves a trace, and the improver learns only from those traces, never from the scores it is
   judged on.

I built the pipeline end to end on a few papers first, kept every failed run, and only then added
self-improvement. The failures were more useful than the successes: almost every check in the verifier
exists because a real run got past the previous version.

## How I built it

One paper goes through these stages. Each stage is persisted and resumable, and each one is a neatlogs span.

- **Watcher.** New arXiv papers with code (arXiv API plus Hugging Face Daily Papers).
- **Triage.** Rules first (GPU signals, large weights, hosted-API keys, repo size), then a cheap model.
  Most fresh ML papers stop here because they need a GPU.
- **Claim sealing.** A model extracts the headline numeric claims with verbatim quotes from the paper text. A
  claim whose quote is not in the text is dropped. The rest are sealed with sha256 before any build starts.
- **Builder.** An agent with shell tools inside a Docker sandbox: uid 1000, all capabilities dropped, CPU and
  memory limits, no host mounts, no credentials. Its only network is an allowlist proxy (PyPI, PyTorch wheels,
  GitHub, Hugging Face, Zenodo, UCI, OpenML). It sees metric names but not values, and numbers in the README
  that match a sealed value are redacted. Its job is to write `/work/paperci_run.sh`, which must recompute the
  metrics with the repo's own code and pass an offline dry run.
- **Verifier.** A different model family. It runs the script again in fresh containers with
  `--network none`, reads only the output files, extracts numbers deterministically (a model is used only for
  ambiguous logs, and its quote must exist in the log), and compares them with the sealed claims.
- **Robustness.** Reruns with other seeds, so a tolerance is calibrated and not guessed.
- **Integrity checks.** Sealed values searched in everything the builder wrote; output files hashed against
  the pristine repo; a run that finishes in seconds while reading result-like files is a replay; a script
  that never touches a repo module is not a reproduction.
- **Packager and oracle.** A passing run becomes a Harbor task. Its oracle replays the builder's steps in a
  clean environment and must earn reward 1, or the task is rejected.
- **Review.** Claims vs measured, the patch the repo needed, a pinned lock file and a `reproduce.sh`.

Around that sit the web app (a concierge agent that talks to the author while the run happens), the
self-improvement loop, and an AWS server that runs it all unattended with its own healers.

## Challenges and how I resolved them

**The builder read instead of running.** Early builds spent most of their turns on `cat`, `ls` and `grep`
and ran out of turns before writing a script. Advice in the prompt did not change this. I added harness
milestones instead: by turn N the agent must have run repo code, by turn M the script must exist, or a
mandatory instruction is injected. Turns per paper on the held-out set went from 16.5 to 11.8.

**My own agent cheated.** On the held-out set one run loaded stored results and finished in four seconds.
Another computed a formula it had written itself and never called the repo. Neither tripped the first
version of the verifier. I added `precomputed_results_replay` and `no_repo_code_path`, and
`scripts/reverify.py` re-judges every stored run with the current rules so old results cannot keep a pass
they would not get today.

**Settings looked like results.** Logs print headers like `--- c2: 6000 steps ---`, and the extractor read
6000 as a measurement. Extraction is now header-aware and claim-id tagged, so two claims that share a metric
name do not steal each other's numbers.

**The improver gave stale advice.** It learned "this host is blocked" from old traces after I had added the
host to the allowlist. The gate now checks every host in a proposed rule against the current allowlist.
Later the policy proposer told the builder that PyPI was blocked, which was never true; the proposer prompt
now states the real network rules.

**An evolution round voided itself.** The round fingerprints everything a candidate must not influence,
including sealed claims. It hashed all sealed claims, so an author's submission in the middle of the night
changed the fingerprint and voided the round. The referee now covers only the benchmark's own claims, and a
round that is interrupted decides on the runs that finished before the change.

**Authors waited behind the robot.** A submitted paper sat in "Queued" while a background evaluation ran. Every
background job is now preemptible: the worker checks for a waiting author every 5 seconds, stops the job's
whole process tree, removes its sandboxes, and redoes the job later without counting it as an attempt.

**"Go ahead" was ignored.** The concierge asks the author a few questions before starting. Replies like
"Go ahead with your plan" were treated as guidance and the job waited for more. Short go-replies now start
the run.

**GitHub rate limits looked like missing repos.** Anonymous GitHub allows 60 API requests an hour. When the
quota ran out, the snapshot recorded "repo unavailable" as a normal result, triage rejected the paper, and the
result was cached. 24 of the first 53 ICML rejections were this. A quota 403 now raises an error, so nothing
is cached, and screening waits for the reset.

**The Factory API key has no chat endpoint.** I wanted all LLM calls on cheap models through the Factory key.
There is no OpenAI-style endpoint, so each call is one `droid exec` in an empty temporary directory, with
built-in skills off and only a TodoWrite tool. Tool calls come back as JSON and are mapped to the OpenAI shape
the agent loop already uses. It costs about 4 to 12 seconds per call, which is fine for an agent that waits on
`pip install` anyway.

**Telemetry is not allowed to take the service down.** The neatlogs managed-prompt API returned 503 for a
while, and a long trace once failed to finalize. Prompt registration is now best-effort with a 15-second cap,
and delivery problems are reported in `/healthz` and as incidents, never raised.

## Learning from community logbooks

Hugging Face's ICML 2026 Open Reproductions has thousands of logbooks: one Hugging Face Space per team per
paper, with notes written while they got the code running. The methods page usually says which dependency
pins worked, where the data actually lives, which script produced the numbers, and what they did without a
GPU. That is exactly the knowledge PaperCI's builder is missing when it fails.

So the nightly improver gets two inputs side by side:

- **(A)** PaperCI's own recurring failures, from its traces (for example 10 proxy refusals, 6 missing
  modules, 4 attribute errors from version drift).
- **(B)** methods notes from 8 high-quality logbooks, of papers where the team verified at least one claim.

It writes general recovery rules that a successful team's tactics suggest for those failures. On the first
try it produced four, including "download and cache data during setup and check the files exist before the
offline run", and "an AttributeError on import usually means version drift: install the pinned version and run
a one-line import test first".

What keeps this honest:

- **Train papers only.** Logbooks are read only for papers outside PaperCI's ICML candidate set, so never a
  benchmark paper and never an eval paper. The eval stays blind.
- **Same gates as every other change.** The rule's regex must match its own failure class and almost nothing
  else; the advice passes the integrity filter and the allowlist check; and a leak gate rejects anything that
  names a source paper, repository, Space, arXiv id or result-like number.
- **Tested before adopted.** The rules become a playbook candidate in the evolution round, next to the
  improver's own candidate, and both are run for real on the benchmark against the current version. At most
  one playbook is adopted per round, the best one, and only if it beats the incumbent by the margin. The canary
  then watches live papers and rolls it back if they get worse.

## What the results contain

On 10 held-out papers taken in queue order, the self-improved builder went from 3 to 5 papers built and run
offline, 1 to 2 faithful reproductions, 0 to 2 claims within tolerance and 0 to 2 oracle-validated tasks, at
$0.18 instead of $0.24 per paper. The integrity checks threw out 3 runs in each arm. Those runs used Claude
Sonnet 4.6 as builder and Amazon Nova Pro as verifier.

The ICML 2026 eval is running now with DeepSeek V4.1 Flash as builder and GLM-5.3-Flash as verifier. Results
are published by paper name as they finish, next to the community verdict, at
[paperci.vercel.app/icml](https://paperci.vercel.app/icml).

## Approaches and trade-offs

I considered letting the builder see the target numbers and asking it to be honest. It is cheaper and finds
the right script faster, but then every pass is suspect. Sealing costs turns; I kept it.

I used a different model family for the verifier, not a second call to the same model, so the grader does not
share the builder's blind spots. With everything on the Factory key the verifier is GLM, which is the same
family as the Hugging Face judge. That is a known bias in the ICML numbers, and it is stated next to them.

I made learned behaviour data (playbook rules and controller knobs) plus one sandboxed policy file with a
strict contract, not free-form code. The improver can make the agent better at its job; it cannot change how
the job is graded.

I chose CPU-only sandboxes so the system is cheap enough to run on every new paper. Most fresh ML papers need
a GPU, so coverage is the cost.

## Limitations and what I would improve

The held-out result is 10 papers, single runs. The ICML comparison is paper-level only; the community's claims
and PaperCI's sealed claims are extracted differently, so claim-level matching is still to do. Coverage is
limited by CPU. Claims are taken from text, so numbers that only appear in figures are missed.

Next I would add GPU sandboxes for a paid tier, match claims across the two systems, run the ICML comparison
with two seeds per paper, and use logbooks to learn not just rules but per-domain environment templates.

The main lesson is simple: an agent that reproduces papers needs to be checked by something it cannot see or
talk to. Most of PaperCI is that something.
