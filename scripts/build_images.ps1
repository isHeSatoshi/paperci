# Builds the PaperCI sandbox images. Usage: powershell -File scripts\build_images.ps1 [-Tag base]
param([string]$Tag = "base")
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
New-Item -ItemType Directory -Force runs | Out-Null
$log = Join-Path $root "runs\build_sandbox_$Tag.log"
"start $(Get-Date -Format o)" | Out-File $log
docker build -q -t paperci/proxy:latest sandbox\proxy *>> $log
docker build --progress=plain -t "paperci/sandbox:$Tag" sandbox\runner *>> $log
"exit=$LASTEXITCODE end $(Get-Date -Format o)" | Out-File -Append $log
