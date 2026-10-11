# PaperCI in the cloud: an agent that runs itself

From 2026-10-11 until 2026-10-16 23:59 IST, PaperCI runs continuously on one EC2 instance in the hackathon AWS
account. Nobody needs to start it. It watches arXiv, reproduces new papers, writes reviews, learns from its own
failures every night, breaks itself on purpose to prove it can recover, and repairs whatever goes wrong.

- Live board (public, anonymized): `https://44-215-70-124.sslip.io/board`
- Author app (invite code required): `https://44-215-70-124.sslip.io/`

## What runs

| Loop | When | What it does |
|---|---|---|
| Author jobs | on submission | Conversation, sandbox rebuild, offline verification, review, repo watch. Always first in the queue. |
| Autopilot | every 90 min when idle | Watch arXiv -> triage + seal claims of up to 6 new papers -> reproduce the freshest never-attempted paper -> private review + anonymized review on the board. |
| Nightly | 03:00 IST | Self-improvement from the last two days of **dev-split** failures (held-out papers never feed the learner), a chaos drill, a daily digest. |
| Watchdog | every 3 min (systemd timer, root) | Docker daemon, egress proxy and internal network, orphaned sandboxes, disk pressure (prunes old build images), web health (restarts after two failed checks). Every repair is logged as an incident. |
| Poweroff | 2026-10-16 18:29 UTC | Stops the instance at the end of the credit window. |

Only one sandbox job runs at a time (`paperci.web` worker), so author jobs, autopilot and nightly work never
compete for the 2 vCPUs and 8 GB of RAM.

## Who heals the healer

The self-healing layer is built to stay simpler than what it heals, and each layer is supervised by a simpler one:

| Failure | What catches it | Proven by drill (2026-10-11, live server) |
|---|---|---|
| Agent process dies (crash, OOM kill) | systemd `Restart=always`, no start limit; `OOMScoreAdjust=-500` so the kernel kills a sandbox first | SIGKILL -> healthy again in 6 s |
| Agent alive but hung (deadlock, stuck job) | Worker heartbeat + per-job deadlines in `/healthz` (503); watchdog restarts after 2 failed checks | SIGSTOP -> detected and restarted |
| A job runs forever | Hard limits (prepare 30 min, run/cycle 2 h, nightly 1 h) kill the whole process tree; orphaned sandboxes are swept | unit-tested |
| A job keeps crashing the agent | Schedule advances before work starts; author jobs retried at most twice; autopilot never re-picks a paper that had any build | unit-tested |
| Watchdog itself broken (bug, bad deploy, broken venv) | Every check is isolated; if the Python watchdog crashes or hangs, a plain-shell fallback healer runs (no Python, no repo imports) | crash injected -> fallback restarted the agent |
| Docker daemon down | Watchdog restarts Docker, recreates proxy and network | Docker stopped -> Docker and egress proxy back |
| Healer making things worse | Fixed list of safe actions (no LLM decides repairs); never prunes images in use or the build cache while a job runs; at most 3 restarts/hour, then one rollback, then it stops and asks for a human | rate limit in code |
| Bad deploy | `deploy/deploy.sh`: deps + import check + offline tests + health check, otherwise automatic rollback; the watchdog can also roll back to the last good commit | syntax error injected -> rejected, rolled back, healthy |
| Guest OS frozen | `kernel.panic=10`, `panic_on_oops`, `softlockup_panic` reboot the VM; all services start at boot | not drilled (would reboot the server) |
| Host hardware failure | EC2 automatic recovery (`AutoRecovery: default`) | AWS-managed |
| Whole server gone | `.github/workflows/uptime.yml` checks `/healthz` from outside AWS every 30 min; a failure emails the repo owner | runs on schedule |

Re-run the drills any time: `bash deploy/healer-drills.sh` (refuses while a paper is running). Results go to the
board under "Who heals the healer".

What is still not covered: an AWS account-level action (the organizers stopping or terminating the instance,
or the SCP changing), and a human not reading the uptime email. CloudWatch alarms are denied in this account.

## Spend controls

- LLM: `PAPERCI_DAILY_CAP` (USD per IST day, counted from deploy time) on top of the existing per-paper,
  per-stage and global hard stops. When the cap is reached the autopilot pauses until midnight and says so on the board.
- AWS: one t3.large (CPU credits in `standard` mode, so no burst charges), 50 GB gp3, no Elastic IP (account limit
  reached; the auto-assigned public IP is stable while the instance runs). About $10.5 for the 5-day window,
  plus about $1.5 for storage and the public IPv4 address.

## Account constraints found by dry runs

Only us-east-1; instance types up to t3.large; root volume at most 50 GB; the `CreatedBy` tag is reserved;
CloudFormation, SSM, SES, Bedrock, Budgets and Cost Explorer are denied; the account's default EBS KMS key is in an
invalid state (an encrypted launch failed with `Client.InvalidKMSKey.InvalidState`), so the root volume is
unencrypted. Nothing sensitive is stored on it beyond `.env` (mode 600). The account is shared with other teams,
so the instance has termination protection, no instance profile, IMDSv2 only, SSH from one operator IP, and
secrets are never put in user data.

## Operating it

```powershell
# deploy a new commit
ssh -i $HOME\.ssh\paperci_aws ubuntu@44.215.70.124 "cd paperci && git pull -q && sudo systemctl restart paperci-web"
# what is it doing?
ssh -i $HOME\.ssh\paperci_aws ubuntu@44.215.70.124 "cd paperci && .venv/bin/python -m paperci.ops events"
# back up its state to backups\
powershell -File scripts\pull_cloud.ps1
```

Infrastructure: `deploy/aws/provision.py` (boto3; CloudFormation is denied in this account), `deploy/bootstrap.sh`,
`deploy/systemd/`, `deploy/Caddyfile` (automatic HTTPS via an sslip.io hostname).
