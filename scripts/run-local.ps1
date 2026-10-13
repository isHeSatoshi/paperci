# PaperCI on your own Windows machine: the same agent as https://paperci.vercel.app, your own LLM.
#   powershell -ExecutionPolicy Bypass -File scripts\run-local.ps1          # set up, check, start
#   powershell -ExecutionPolicy Bypass -File scripts\run-local.ps1 -Check   # set up and check only
# Needs Python 3.11+ and Docker Desktop (Linux containers). Then open http://127.0.0.1:8000
param([switch]$Check)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)

if (-not (Test-Path .venv)) {
    $py = if (Get-Command py -ErrorAction SilentlyContinue) { 'py' } else { 'python' }
    Write-Host "== creating .venv"
    if ($py -eq 'py') { py -3 -m venv .venv } else { python -m venv .venv }
}
Write-Host "== installing requirements"
.venv\Scripts\python.exe -m pip install -q --upgrade pip
.venv\Scripts\python.exe -m pip install -q -r requirements.txt
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if (-not (Test-Path .env)) {
    Copy-Item deploy\local\local.env.example .env
    Write-Host "== created .env from deploy\local\local.env.example (Ollama preset). Edit it to pick your models."
}

Write-Host "== preflight"
$env:PYTHONIOENCODING = 'utf-8'
.venv\Scripts\python.exe -m paperci.localrun check --build
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
if ($Check) { exit 0 }

Write-Host "== PaperCI is starting on http://127.0.0.1:8000 (Ctrl+C to stop)"
.venv\Scripts\python.exe -u -m paperci.web
