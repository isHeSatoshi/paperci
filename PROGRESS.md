# PROGRESS (resume point for any new session)

## State (2026-10-11 ~10:15 IST)
- Pipeline complete end to end and exercised on real papers: watcher -> triage -> sealed claims -> builder
  (v1/v2) -> offline verifier -> robustness -> verdict (with integrity flags) -> Harbor packager -> oracle replay
  -> trainability -> registry/dashboard.
- Self-improvement: playbook gen2 (rules + enforced controller), image gen1. Held-out v1 vs v2 done on 5 papers.
- Self-audit + re-verification done (`scripts/reverify.py`); clean packages re-built (`scripts/repackage.py`).
- Registry: 2 oracle-validated tasks (held-out 09280-class toy-scale v2, dev copula toy-scale), 4 rejected.
- Chaos run done (proxy down -> self-heal -> recovered). Trace `14ed5fe7f083b2b1ff4c98fbbf705223`.
- Docs generated: docs/evidence.md, docs/unit-economics.md, dashboards (registry/index.html private,
  docs/dashboard_public.html anonymized). Spend $5.44 of $20.

## Cloud (2026-10-11 evening IST onward)
- The primary copy now runs on EC2 `i-0184830ca8b082fb9` (44.215.70.124, https://44-215-70-124.sslip.io). It
  started from a copy of the local DB, runs and registry. Local runs after this point diverge from it.
- SSH: `ssh -i $HOME\.ssh\paperci_aws ubuntu@44.215.70.124` (only from the operator IP in the security group).
- Deploy: `git pull` on the server + `sudo systemctl restart paperci-web`. State: `python -m paperci.ops events`.
- Backup: `scripts/pull_cloud.ps1`. Teardown: `deploy/aws/provision.py down`. Details: docs/cloud.md.

## How to resume
1. Docker: if `docker version` fails, check C: free space first (it filled once). Restart Docker Desktop via
   `Invoke-CimMethod Win32_Process Create` (not Start-Process from the agent shell).
2. `python -m paperci stats` / `budget`; `python scripts/build_evidence.py`; `python scripts/economics.py --md`.
3. Long runs: launch via WMI (`cmd /c "... > runs\logs\X.log 2>&1"`); logs in runs/logs/.

## Next steps (if more time)
1. More held-out papers (queue order) with v2 to tighten the before/after sample (n=5 is small).
2. Improver gen3 from the new integrity flags on dev (penalize replay / non-repo code paths in the playbook).
3. GPU worker class so GPU-scale papers stop being triage rejects.

## Open items (human)
See HUMAN_TASKS.md: trace share links, Entire repo import check, cfo.ai plan, demo video, posts, submission.
