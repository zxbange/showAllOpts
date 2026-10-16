$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$pythonExe = Join-Path $projectRoot 'backend/.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw 'Python environment not found. Run scripts/setup.ps1 first.'
}
& $pythonExe (Join-Path $projectRoot 'scripts/dev.py')
exit $LASTEXITCODE
