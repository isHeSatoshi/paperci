# Demo video: script and shot list (target 3:00)

The judging line is "how clearly the demo video explains the agent and your use of neatlogs, Entire and cfo.ai".
So the video follows one story: a paper goes in, PaperCI checks its claims, something breaks, neatlogs shows it,
the agent fixes itself, and the fix is proven. Record at 1920x1080, browser zoom 110%, one tab per shot,
opened before you start. Speak slowly; every line below is about 8-12 seconds.

## 0:00-0:15 Hook (shot: landing page `https://paperci.vercel.app/`)
"Most ML papers ship code, and nobody checks that the code produces the numbers in the paper. Reviewers don't
have time. PaperCI is an agent that does it: CI for research papers."

## 0:15-0:55 The agent, one run (shots: `/job/...` of the demo job, then the review page)
Use the finished demo job on 2507.12843 (`docs/img/web-pass.png` is its end state). Scroll the chat.
- "I paste an arXiv link. The agent reads the paper and seals its headline claims with a hash, so the engineer
  agent never sees the numbers it has to hit."
- "It rebuilds the repo from scratch in a locked-down Docker sandbox: non-root, no credentials, the internet only
  through an allowlist proxy."
- "Then it reruns the experiment three times with the network switched off, and a different model family checks
  every claim. Here: three of three claims match at reduced scale." (show the review page)

## 0:55-1:30 It runs on its own and checks real papers (shots: `/board`, then `/icml`)
- "This runs 24/7 on AWS without anyone watching. The board is written by the agent itself: new arXiv papers,
  repairs, chaos drills."
- "We pointed it at 18 ICML 2026 papers that Hugging Face's community already reproduced, without showing it their
  verdicts. Honest result: it builds 11 of 18 on a 2-CPU box, and none reach a full-scale verdict yet; this
  page says so."

## 1:30-2:20 neatlogs + Entire: it finds its own bugs (shots: neatlogs dashboard, then `/judges#loop`)
- neatlogs, Traces: open trace `a3018a6313414b1ad77c8db639be57f0`. "Every run is one trace: the paper goes in,
  the verdict comes out. This one is red: the builder crashed with 'No closing quotation'."
- neatlogs, Incidents: "PaperCI publishes its own failure clusters as incidents. This one hit four builds."
- `/judges#loop`: "That was our harness, not the agent: a quoting bug. Here is the fix commit, it carries the
  Entire checkpoint of the session that wrote it, and this column is measured live: zero occurrences since."
- Same table: "Not every flag is a bug. Here neatlogs flagged an integrity failure, and it was right: the agent had
  simulated data instead of running the repo. That verdict stays."
- Detections page: "We added six detections of our own, from 'builder ran out of budget' to 'claims verified'."

## 2:20-2:45 Self-improvement with a referee (shot: `/judges#improved`, then `docs/evolution.md` on GitHub)
- "It also improves itself. An evolution loop proposes new playbooks and control policies, runs each one for
  real on a fixed benchmark, and keeps it only if it scores higher. The verifier and sealed claims are pinned by
  hash, so it can't cheat by changing the judge."
- "On the same 10 held-out papers: built went from 3 to 5, and cost per attempt dropped from 24 to 18 cents."

## 2:45-3:00 cfo.ai and close (shot: cfo.ai plan, then `/judges` top)
- "cfo.ai turns the measured cost per run into pricing and runway." (one sentence on the plan you made)
- "Everything we claim is on this page, re-checked live, including what we did not prove. PaperCI. Thanks."

## Before you record
- [ ] Share these traces in neatlogs (Share button on each trace) and paste the public links into
      `docs/judges-links.json` under `shared_traces`: `a3018a6313414b1ad77c8db639be57f0`,
      `29c2dee743f80c15c5e43f9f2b231bdb`, `59a1c806c9921f4fe1723f14518702da`.
- [ ] cfo.ai plan done; link in `docs/judges-links.json` as `cfo_url`.
- [ ] After recording, the video link goes in `demo_url` too; `/judges` picks all of these up on its own.
- [ ] Use `?redact=1` on any `/job/...` page that shows an author's paper.
