# Back up the cloud agent's state (SQLite snapshot, runs, registry, playbook) to backups\<timestamp>.tgz.
# The hackathon AWS account is shared, so the server is treated as disposable.
# Usage: powershell -File scripts\pull_cloud.ps1 [-Ip 44.215.70.124]
param([string]$Ip = "44.215.70.124", [string]$Key = "$HOME\.ssh\paperci_aws")
$root = Split-Path -Parent $PSScriptRoot
$dest = Join-Path $root "backups"
New-Item -ItemType Directory -Force $dest | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmm"
$remote = "cd ~/paperci && sqlite3 data/paperci.sqlite "".backup /tmp/paperci-backup.sqlite"" && " +
          "tar -czf /tmp/paperci-backup.tgz -C ~/paperci runs registry data/playbook.json data/playbook_history " +
          "-C /tmp paperci-backup.sqlite && rm /tmp/paperci-backup.sqlite"
ssh -i $Key -o BatchMode=yes "ubuntu@$Ip" $remote
if ($LASTEXITCODE -ne 0) { Write-Error "remote snapshot failed"; exit 1 }
scp -i $Key -q "ubuntu@${Ip}:/tmp/paperci-backup.tgz" (Join-Path $dest "cloud-$stamp.tgz")
ssh -i $Key -o BatchMode=yes "ubuntu@$Ip" "rm -f /tmp/paperci-backup.tgz"
Get-Item (Join-Path $dest "cloud-$stamp.tgz") | Select-Object Name, Length
