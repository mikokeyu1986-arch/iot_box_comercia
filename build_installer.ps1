$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = "C:\Users\Miko win\AppData\Local\Programs\Python\Python311\python.exe"
$pyi = Join-Path (Split-Path $py) "Scripts\pyinstaller.exe"
$build = Join-Path $root "build"
if (Test-Path $build) { Remove-Item -LiteralPath $build -Recurse -Force }
New-Item -ItemType Directory -Path $build | Out-Null

& $pyi --noconfirm --clean --onedir --windowed --name gui_app `
  --icon "$root\assets\iotbox-icon.ico" `
  --add-data "$root\web;web" --add-data "$root\certs;certs" `
  --add-data "$root\runtime_config.json;." --hidden-import pystray --hidden-import pywebview `
  "$root\gui_app.py"

& $pyi --noconfirm --clean --onedir --console --name run_http `
  --icon "$root\assets\iotbox-icon.ico" `
  --add-data "$root\web;web" --add-data "$root\certs;certs" `
  --add-data "$root\runtime_config.json;." --collect-all uvicorn --collect-all fastapi `
  "$root\run_http.py"

$iscc = "C:\Program Files\Inno Setup 6\ISCC.exe"
& $iscc (Join-Path $root "installer\IOTBOX.iss")
Write-Host "Installer generated in $root\release\IOTBOX-SETUP.exe"
