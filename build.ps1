$ErrorActionPreference = 'Stop'

$project = Split-Path -Parent $MyInvocation.MyCommand.Path
$deps = Join-Path $project '.builddeps'
$bundledPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$systemPython = Get-Command python.exe -ErrorAction SilentlyContinue
$python = if (Test-Path $bundledPython) {
    $bundledPython
} elseif ($systemPython) {
    $systemPython.Source
} else {
    throw '未找到 Python 3，无法打包。'
}

if (-not (Test-Path (Join-Path $deps 'PyInstaller'))) {
    & $python -m pip install --target $deps pyinstaller websocket-client
}

$env:PYTHONPATH = "$deps;$project"
& $python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --noconsole `
    --name '领星数字SKU清理助手' `
    --paths $deps `
    (Join-Path $project 'app.py')

Write-Host "Built: $(Join-Path $project 'dist\领星数字SKU清理助手.exe')"
