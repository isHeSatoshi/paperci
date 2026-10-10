# Detached runner: powershell -File scripts\run_bg.ps1 -Name dev-v1-a -Args "run --label dev-v1 --variant v1 --ids 2501.14570"
param([string]$Name, [string]$CliArgs)
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
New-Item -ItemType Directory -Force runs\logs | Out-Null
$log = Join-Path $root "runs\logs\$Name.log"
$env:PYTHONIOENCODING = "utf-8"
"start $(Get-Date -Format o) :: $CliArgs" | Out-File $log -Encoding utf8
$argv = $CliArgs -split ' '
& "$root\.venv\Scripts\python.exe" -m paperci @argv *>> $log
"exit=$LASTEXITCODE end $(Get-Date -Format o)" | Out-File -Append $log -Encoding utf8
