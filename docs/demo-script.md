# Demo script (3:30) and shot list: "CodeRabbit for research papers"

The product is the web app (`python -m paperci.web`, http://127.0.0.1:8000). Record at 1080p. Use your own
email; pick a paper you are comfortable showing (a verdict about a named paper appears on screen, so prefer
a paper you co-authored or one whose review is positive, or blur the title). Fallback stills are in `docs/img/`.

Prep: start the web app; have one finished conversation open in a tab (shows the review + "watch my repo"),
and submit a fresh one at the start of recording. A run takes 10-20 minutes; speed it up in editing.

## 0:00-0:20 Hook
- Shot: a GitHub PR with a CodeRabbit comment. Cut to the PaperCI landing page.
- VO: "Every pull request gets a bot that runs the tests and reviews the change. Papers get nothing; most
  papers' code is never re-run. PaperCI is CodeRabbit for papers, and it's an agent you talk to."

## 0:20-0:45 Submit
- Shot: type email, switch to "Upload a file", drop a LaTeX source `.tar.gz` (or a PDF), paste the repo, tick
  consent, "Start verification". Redirects to the private conversation page.
- VO: "Any academic format: PDF, LaTeX source, Word, HTML. Plus the code."

## 0:45-1:30 The agent talks to you
- Shot: the agent's first message: the claims it found with the paper's values, what it saw in the repo, its
  plan, and 1-3 questions (which claim matters, the command that produces a number, a reachable data mirror).
- Shot: type a reply ("c2 matters most; use train.py darcy"). The agent confirms what it will do.
- VO: "It reads the paper and the repo, seals every headline number so the engineer agent never sees them,
  and asks only what would change its plan. Your answer goes to the engineer with any numbers redacted."

## 1:30-2:15 It works, narrates, connects
- Shot: right panel: progress steps ticking, Connections chips (GitHub, PyPI, PyTorch wheels; a red blocked
  host), the chat narrating: "Cloned your repo...", "I've read enough; switching to running it", "Your code
  ran end to end with the network switched off", "reran it in fresh containers: exit codes [0, 0, 0]".
- VO: "A fresh locked-down container, only an allowlist of code and data hosts, and a policy it learned from
  earlier papers: stop reading, start running."

## 2:15-2:45 It comes back when it's stuck
- Shot: the agent's question after a blocked attempt ("I couldn't get your code to produce the numbers within
  my step budget... which command produces your headline number?"), the outbox email, the reply, "Starting
  attempt 2 with what you told me."
- VO: "When it hits something only you can fix, it asks, by chat and email, and retries with your answer."

## 2:45-3:10 The review
- Shot: the embedded review: pipeline strip, claims vs measured with honest statuses, checks (anti-cheat),
  suggestions for the authors, reproduce-it-yourself, badge. Then "Yes, watch my repo".
- Shot (still): `docs/img/review-caught.png`, "Computed, not replayed" failed: the judge caught a shortcut.
- VO: "Claim by claim: what the paper says, what the code produced. It catches shortcuts, including its own
  agent's. And it keeps watching: every new commit gets re-verified."

## 3:10-3:30 Proof + sponsors
- Shot: `docs/evidence.md` held-out table: on 10 papers it never tuned on, built 3 -> 5, faithful reproductions
  1 -> 2, validated tasks 0 -> 2, cost per paper $0.24 -> $0.18. neatlogs trace pair (BEFORE
  `294209f0b2d1b60bfc1509fdbece2f1b`, AFTER `4e85d604ac7a5d343950ee923f573502`). Entire checkpoint page.
- VO: "It improves itself from its neatlogs traces, every session is an Entire checkpoint, and at about
  20 cents an attempt the cfo.ai plan works. Today deterministic code on a CPU; next, GPUs and simulators."
