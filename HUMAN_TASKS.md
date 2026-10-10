# HUMAN_TASKS (things only a person can do)

Batched for you. Each item has exact steps. Nothing here has been done by the agent.
Status legend: TODO / DONE (mark it yourself).

## 1. neatlogs CLI readback (device approval) — TODO, ~3 min
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

## 2. neatlogs dashboard: grab the before/after trace links — TODO, ~5 min
1. Open https://app.neatlogs.com -> project -> Traces.
2. Search for the trace IDs listed in `docs/evidence.md` (section "Trace IDs"). Workflow name: `paperci-reproduce-paper`.
3. For the BEFORE and AFTER trace pair and the failure-recovery trace, copy the share link (or take a screenshot) and paste into `docs/evidence.md` next to each ID.

## 3. Entire: connect the GitHub repo — TODO, ~5 min
The local checkpoints exist (see hackathon.md). For a public checkpoint link the repo must be on GitHub and imported in Entire.
1. Go to https://entire.io -> Sign in with GitHub (account `isHeSatoshi`) -> authorize the Entire GitHub App.
2. Install the Entire GitHub App on the account/org that owns the PaperCI repo (onboarding prompts you). Select the `paperci` repository.
3. Back in the terminal: `cd D:\Project\neathack; entire status` should show checkpoints syncing after the next `git push`.
4. Open repo -> branch `main` -> the commit listed in hackathon.md -> checkpoint. Confirm Sessions and Changes tabs load.
5. The shareable link format is `https://entire.io/gh/OWNER/REPO/commit/FULL_SHA` (exact link prepared in hackathon.md).

## 4. cfo.ai business plan — TODO, ~20 min
1. Sign up at https://cfo.ai/neathacks (card required). IMMEDIATELY go to Settings -> Billing -> cancel subscription (per hackathon page; you won't be billed).
2. Create a new plan "PaperCI". Enter the numbers from `docs/unit-economics.md`, section "cfo.ai entry sheet" (pricing tiers, COGS per attempt, infra, headcount, runway).
3. Export/share the plan (PDF or link) and save it as `docs/cfo-plan.pdf` (or paste the link into hackathon.md, Submission section).

## 5. Demo video — TODO, ~30 min
Script and shot list: `docs/demo-script.md`. Record screen + voice, 3-4 minutes. Use the public (anonymized) dashboard `docs/dashboard_public.html` on screen, not the private one, so no verdict about a named paper is published.

## 6. Building in public posts — TODO
Drafts in `docs/posts.md` (tagged #neatHack). Post them yourself on X/LinkedIn/Discord #neatHack and paste the links into hackathon.md.

## 7. Final submission (Mon 2026-10-12, 7:00-11:55 PM IST) — TODO
Quote the official neatlogs post on X with: team, project name, public repo URL, demo video link. (Agent is not authorized to post or submit.)
