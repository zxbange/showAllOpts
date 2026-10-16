param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$backendRoot = Join-Path $projectRoot 'backend'
$venvPython = Join-Path $backendRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    & $Python -m venv (Join-Path $backendRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create the Python environment.' }
}
$requirements = Join-Path $backendRoot 'requirements.lock.txt'
if (-not (Test-Path -LiteralPath $requirements)) {
    $requirements = Join-Path $backendRoot 'requirements-dev.txt'
}
& $venvPython -m pip install -r $requirements
if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed.' }
& $venvPython (Join-Path $PSScriptRoot 'configure_database.py')
if ($LASTEXITCODE -ne 0) { throw 'Check APP_ENV_FILE or backend/.env configuration.' }
Push-Location (Join-Path $projectRoot 'frontend')
try {
    npm.cmd ci --no-fund --no-audit
    if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency installation failed.' }
} finally { Pop-Location }
Write-Host 'Setup complete. Run ./start.ps1, then visit http://127.0.0.1:5173'
