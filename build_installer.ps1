$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = (Get-Command python.exe -ErrorAction Stop).Source
$pyi = Join-Path (Split-Path $py) "Scripts\pyinstaller.exe"
if (-not (Test-Path $pyi)) {
  & $py -m pip install --disable-pip-version-check pyinstaller
  if ($LASTEXITCODE -ne 0 -or -not (Test-Path $pyi)) {
    throw "PyInstaller installation failed."
  }
}
$build = Join-Path $root "build"
if (Test-Path $build) { Remove-Item -LiteralPath $build -Recurse -Force }
New-Item -ItemType Directory -Path $build | Out-Null

& $pyi --noconfirm --clean --onedir --windowed --name gui_app `
  --icon "$root\assets\iotbox-icon.ico" `
  --add-data "$root\web;web" --add-data "$root\certs;certs" `
  --add-data "$root\redsys;redsys" `
  --add-data "$root\runtime_config.json;." --hidden-import pystray --hidden-import pywebview `
  "$root\gui_app.py"

# GUI reads the REDSYS configuration relative to gui_app.exe, so keep a
# writable external copy beside the executable (not only in _internal).
Copy-Item "$root\redsys" (Join-Path $root "dist\gui_app\redsys") -Recurse -Force

# The frozen GUI cannot use its own EXE as a Python interpreter.  Build the
# REDSYS HTTP service separately, while keeping its editable config/runtime
# files next to gui_app.exe.
& $pyi --noconfirm --clean --onedir --console --name redsys_server `
  --icon "$root\assets\iotbox-icon.ico" `
  --add-data "$root\redsys;redsys" `
  "$root\redsys\server\main.py"

& $pyi --noconfirm --clean --onedir --console --name run_http `
  --icon "$root\assets\iotbox-icon.ico" `
  --add-data "$root\web;web" --add-data "$root\certs;certs" `
  --add-data "$root\runtime_config.json;." --collect-all uvicorn --collect-all fastapi `
  "$root\run_http.py"

# HTTPS is launched by gui_app.exe from the runtime directory.  Keep this as a
# separate executable (rather than falling back to Python on customer PCs) so
# the installer always supports the protocol selected in the GUI.
& $pyi --noconfirm --clean --onedir --console --name run_https `
  --icon "$root\assets\iotbox-icon.ico" `
  --add-data "$root\web;web" --add-data "$root\certs;certs" `
  --add-data "$root\runtime_config.json;." --collect-all uvicorn --collect-all fastapi `
  "$root\run_https.py"

if (-not (Test-Path (Join-Path $root "dist\run_https\run_https.exe"))) {
  throw "HTTPS runtime build failed: dist\\run_https\\run_https.exe was not created."
}
if (-not (Test-Path (Join-Path $root "dist\redsys_server\redsys_server.exe"))) {
  throw "REDSYS runtime build failed: dist\redsys_server\redsys_server.exe was not created."
}
New-Item -ItemType Directory -Path (Join-Path $root "dist\gui_app\runtime") -Force | Out-Null
Copy-Item (Join-Path $root "dist\redsys_server") (Join-Path $root "dist\gui_app\runtime\redsys_server") -Recurse -Force

$isccCandidates = @(
  "C:\Program Files\Inno Setup 6\ISCC.exe",
  "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
)
$iscc = $isccCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) {
  throw "Inno Setup 6 compiler (ISCC.exe) was not found."
}
& $iscc (Join-Path $root "installer\IOTBOX.iss")
Write-Host "Installer generated in $root\release\IOTBOX-SETUP.exe"
