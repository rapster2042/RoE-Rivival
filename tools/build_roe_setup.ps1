# Rebuilds RoE-Setup.exe (first-run setup) via PyInstaller from
# .tools\python — no system Python required.
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$py = Join-Path $repo '.tools\python\python.exe'
& $py -m PyInstaller --onefile --console --name RoE-Setup `
    --paths (Join-Path $repo 'tools') `
    --distpath (Join-Path $repo 'dist') `
    --workpath (Join-Path $repo '.pybuild') `
    --specpath (Join-Path $repo '.pybuild') `
    (Join-Path $repo 'tools\setup_identity.py')
if ($LASTEXITCODE -ne 0) { throw "PyInstaller exited with code $LASTEXITCODE" }
Copy-Item (Join-Path $repo 'dist\RoE-Setup.exe') (Join-Path $repo 'RoE-Setup.exe') -Force
Write-Host "Done: $(Join-Path $repo 'RoE-Setup.exe') - place it next to the project and run once."
