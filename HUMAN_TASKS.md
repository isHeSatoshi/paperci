# HUMAN_TASKS (things only a person can do)

Batched for you. Each item has exact steps. Nothing here has been done by the agent.
Status legend: TODO / DONE (mark it yourself).

## A. More LLM credits for the always-on agent — TODO (you said you can get more), ~5 min
The cloud agent is capped at $1.50/day (`PAPERCI_DAILY_CAP`) because the key has about $11.67 left of $20, and
our own hard stop is $16. At that cap it reproduces about 5 papers a day. Measured costs: about $0.18-0.25 per
reproduction attempt, about $0.05 per cycle to triage and seal new papers, about $0.10 per nightly learning run.
Running the full schedule (a cycle every 90 min) costs about $4-5/day.
Ask the organizers for: **+$30 on the same LiteLLM key** (same models: claude-sonnet-4.6, claude-haiku,
nova-pro, nova-micro; no opus), valid through Fri 2026-10-16. That covers about 5 days at $4.5/day plus a
demo-day reserve. Then tell the agent "credits added" and it will raise `PAPERCI_GLOBAL_BUDGET`,
`PAPERCI_HARD_STOP` and `PAPERCI_DAILY_CAP` on the server.

## B. Invite codes for the public app — share as you like
Submissions on https://44-215-70-124.sslip.io need an invite code (each run spends LLM budget and runs public
code). The codes are in `%TEMP%\paperci-deploy\codes.txt` on this machine and in the server's `.env`.
They are not in git. Give one to judges and keep one for yourself. The live board needs no code.

## C. The cloud server — nothing to do, FYI
It stops itself on 2026-10-16 23:59 IST. If you want it gone earlier (or after), run
`python deploy/aws/provision.py down` with AWS credentials, or ask the agent to tear it down. Run
`powershell -File scripts\pull_cloud.ps1` before that to keep a copy of everything it did.

## 0. Disk space on C: (recommended) — TODO, ~10 min
C: ran out of space during the run (Docker died). PaperCI's stale images were deleted, but Docker's disk file
`%LOCALAPPDATA%\Docker\wsl\disk\docker_data.vhdx` (39 GB) does not shrink on its own. To reclaim host space:
Docker Desktop -> Settings -> Resources -> Advanced -> move "Disk image location" to D:, or quit Docker,
run `wsl --shutdown`, then in an admin PowerShell `Optimize-VHD -Path <vhdx> -Mode Full` (needs Hyper-V tools).
Also `uv cache prune` reclaims unused entries of the 60 GB uv cache (your call; not done by the agent).

## 1. neatlogs CLI readback (device approval) — DONE (you approved it; CLI readback works)
Needed so the agent can read traces back from the terminal (SDK ingestion already verified by Doctor probe).
1. In PowerShell (Node 22 is installed):
   `npm install --global neatlogs-cli`
2. Run login without the SDK key in the environment:
   ```
   $k=$env:NEATLOGS_API_KEY; Remove-Item Env:NEATLOGS_API_KEY -ErrorAction SilentlyContinue
   neatlogs --host https://app.neatlogs.com --profile setup-https---app-neatlogs-com auth login --device
   ```
3. Open the printed link in your browser, approve the code.
4. Check: `neatlogs --host https://app.neatlogs.com --profile setup-https---app-neatlogs-com --project da3f6fba-4dfc-47d7-9960-3e3caedf7360 traces list --limit 5`
5. Tell the agent "neatlogs CLI approved".

## 2. neatlogs dashboard: grab the before/after trace links — OPTIONAL now
Agent status: evidence is read back via the CLI into docs/evidence.md (IDs + numbers). The CLI cannot create
share links (sharing is read-only) and the OAuth login has read scopes only, so tagging/commenting traces
returned FORBIDDEN. neatlogs ingestion is also currently degraded (Doctor probe: TRACE_READBACK_TIMEOUT at
pii_redaction); details for the neatlogs team in `docs/neatlogs-feedback.md` (worth posting in their Discord).
If you ever want links/screenshots for the video, the steps are:
1. Open https://app.neatlogs.com -> project -> Traces.
2. Search for these trace IDs (all listed in `docs/evidence.md`):
   - BEFORE (held-out, v1): `294209f0b2d1b60bfc1509fdbece2f1b`
   - AFTER (same paper, v2): `4e85d604ac7a5d343950ee923f573502`
   - Failure + recovery (chaos): `14ed5fe7f083b2b1ff4c98fbbf705223`
3. Copy each share link (or take a screenshot) and paste it into `hackathon.md` (Results) next to the ID.
   Note: some traces show `dlq` finalization in neatlogs (documented in evidence.md section 6). The chaos
   trace is one of them. If it does not render, show the recovery from `docs/evidence.md` section 4 and
   `runs/logs/dev-chaos.log` instead, and consider reporting the DLQ to neatlogs in Discord.

## 3. Entire: connect the GitHub repo — TODO if not done yet, ~5 min
The repo is pushed to https://github.com/isHeSatoshi/paperci (public after the final secret scan) with the
`entire/checkpoints/v1` ref. Checkpoint IDs and commit links are in hackathon.md ("Entire checkpoints").
1. Go to https://entire.io -> Sign in with GitHub (account `isHeSatoshi`) -> authorize the Entire GitHub App.
2. Install the Entire GitHub App on the account/org that owns the PaperCI repo (onboarding prompts you). Select the `paperci` repository.
3. Back in the terminal: `cd D:\Project\neathack; entire status` should show checkpoints syncing after the next `git push`.
4. Open repo -> branch `main` -> the commit listed in hackathon.md -> checkpoint. Confirm Sessions and Changes tabs load.
5. The shareable link format is `https://entire.io/gh/OWNER/REPO/commit/FULL_SHA` (exact link prepared in hackathon.md).

## 4. cfo.ai business plan — TODO, ~20 min
1. Sign up at https://cfo.ai/neathacks (card required). IMMEDIATELY go to Settings -> Billing -> cancel subscription (per hackathon page; you won't be billed).
2. Create a new plan "PaperCI". Enter the numbers from `docs/unit-economics.md`, section "cfo.ai entry sheet" (COGS ~$0.21 per attempt measured+assumed, 5 attempts per validated task on held-out, pricing hypotheses, $300/month infra, 2 founders). Mark prices as hypotheses.
3. Export/share the plan (PDF or link) and save it as `docs/cfo-plan.pdf` (or paste the link into hackathon.md, Submission section).

## 4b. Try the product yourself — 5 min (+ a 10-20 min run)
1. `cd D:\Project\neathack; .venv\Scripts\python.exe -m paperci.web` and open http://127.0.0.1:8000
2. Submit with your email + an arXiv link or a paper file (PDF / LaTeX .tex or source .zip/.tar.gz / .docx / HTML / MD) + the GitHub repo.
3. Talk to the agent in the chat (or click "Go ahead with your plan"). Each run spends about $0.15-0.30 of the LLM key;
   the app refuses new submissions once total spend passes $15 (`PAPERCI_WEB_SPEND_CAP`).
4. Emails are written to `registry/web/outbox/*.eml` (no mail provider is connected). To make it public you would need
   hosting + a mail provider; the app binds to localhost only on purpose.

## 5. Demo video — TODO, ~30 min
Script and shot list: `docs/demo-script.md`. Record screen + voice, 3-4 minutes. Use the public (anonymized) dashboard `docs/dashboard_public.html` on screen, not the private one, so no verdict about a named paper is published.

## 6. Building in public posts — TODO
Drafts in `docs/posts.md` (tagged #neatHack). Post them yourself on X/LinkedIn/Discord #neatHack and paste the links into hackathon.md.

## 7. Final submission (Mon 2026-10-12, 7:00-11:55 PM IST) — TODO
Quote the official neatlogs post on X with: team, project name, public repo URL, demo video link. (Agent is not authorized to post or submit.)
