# HUMAN_TASKS (things only a person can do)

Updated 2026-10-12 17:15 IST. Everything the agent could do itself is done. Status: TODO / DONE.

## Tonight, before 11:55 PM IST

1. **Demo video** — TODO. Script: `docs/demo-script.md`. For the live part, submit arXiv `2507.12843` on
   https://paperci.vercel.app (repo is found automatically). It finishes in about 3-5 minutes and matched 3 of 5
   claims last time. Open https://paperci.vercel.app/judges and /icml for the rest. Upload the video
   (YouTube unlisted or Loom) and give the link to the agent (it goes into `docs/judges-links.json` -> `demo_url`).
2. **cfo.ai plan** — TODO. Plan link goes into `docs/judges-links.json` -> `cfo_url`.
3. **Share 3 neatlogs traces** — TODO, ~3 min. neatlogs -> Traces -> open each -> Share -> copy link, then give the
   links to the agent (they go into `shared_traces`, and /judges shows them):
   `a3018a6313414b1ad77c8db639be57f0`, `29c2dee743f80c15c5e43f9f2b231bdb`, `59a1c806c9921f4fe1723f14518702da`.
4. **neatlogs incidents: mark the fixed ones Resolved** — optional, ~2 min. The public API can only publish
   incident snapshots; "Resolved" is a dashboard action. The 6 under "Investigated" are the clusters PaperCI
   closed with an empty snapshot after a fix (titles say "fixed in <commit>, none since" or "no unrecovered
   occurrences"). Open each and mark it Resolved. Leave the 3 Open ones (missing_module, turn_budget_exhausted,
   data_missing): they are live agent-behaviour clusters that feed the evolution loop.
5. **Entire: GitHub app connected?** — check, ~2 min. https://entire.io -> repo `isHeSatoshi/paperci` -> a recent
   commit -> checkpoint loads. If not: sign in with GitHub, install the Entire GitHub App on the repo.
6. **Final submission** (7:00-11:55 PM IST) — TODO. Quote the official neatlogs post on X with: team, project
   name (PaperCI), repo https://github.com/isHeSatoshi/paperci, demo video link, and
   https://paperci.vercel.app/judges. The agent is not allowed to post or submit for you.

Done already: building-in-public post (https://x.com/isHeSatoshi/status/2109565183140274613, linked on /judges),
app open to the public, spend ceilings sized for the Factory key, invite codes removed.

## Only if the board asks: approve or reject an evolved control policy, ~5 min
Playbooks are adopted automatically; a rewritten builder policy (code) waits for you. None is waiting now.
```
ssh -i $env:USERPROFILE\.ssh\paperci_aws ubuntu@44.215.70.124
cd ~/paperci; .venv/bin/python -m paperci.evolve pending      # id, bench score, file path
less data/evolve/candidates/<id>/builder_policy.py             # read what it does
.venv/bin/python -m paperci.evolve approve <id>                # or: reject <id>
```

## After the event
- Revoke the Factory API key: https://app.factory.ai/settings/api-keys (it was pasted in chat; stored only in
  `.env` here and on the server).
- Revoke the neatlogs service token `paperci-server-b` (neatlogs -> service accounts -> paperci-server); it expires
  on its own around 19 Oct.
- Revoke the GitHub token used for pushes.
- The AWS server stops itself on 2026-10-16 23:59 IST. To keep a copy first: `powershell -File scripts\pull_cloud.ps1`.
  To remove it: `python deploy/aws/provision.py down`.
- Optional disk cleanup on C: Docker's `%LOCALAPPDATA%\Docker\wsl\disk\docker_data.vhdx` does not shrink on its
  own (Docker Desktop -> Settings -> Resources -> Advanced -> move the disk image to D:).
