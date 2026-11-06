$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPython = Join-Path $ProjectDir ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $VenvPython)) {
    python -m venv (Join-Path $ProjectDir ".venv")
}

& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -r (Join-Path $ProjectDir "requirements.txt")
& $VenvPython -m PyInstaller `
    --noconfirm `
    --onedir `
    --windowed `
    --name "心率挂件" `
    --collect-all bleak `
    --collect-all winrt `
    --distpath (Join-Path $ProjectDir "dist") `
    --workpath (Join-Path $ProjectDir "build") `
    --specpath (Join-Path $ProjectDir "build") `
    (Join-Path $ProjectDir "heart_overlay.pyw")

Write-Host "构建完成：$(Join-Path $ProjectDir 'dist\心率挂件')"
