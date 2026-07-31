@echo off
setlocal

set "ROOT=%~dp0"
set "SCRIPT=%ROOT%run_https.py"
set "PORT=8389"
set "LOG_DIR=%ROOT%logs"
set "STDOUT_LOG=%LOG_DIR%\https_stdout.log"
set "STDERR_LOG=%LOG_DIR%\https_stderr.log"
set "PYTHON_EXE="

rem Try common Python installation paths first (portable across users)
for %%P in (
    "%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    "%ProgramFiles%\Python\Python314\python.exe"
    "%ProgramFiles%\Python\Python313\python.exe"
    "%ProgramFiles%\Python\Python312\python.exe"
    "%ProgramFiles%\Python\Python311\python.exe"
) do (
    if exist %%P (
        set "PYTHON_EXE=%%~P"
        goto :found_python
    )
)
where py >nul 2>nul && set "PYTHON_EXE=py" && goto :found_python
where python >nul 2>nul && set "PYTHON_EXE=python" && goto :found_python
:found_python
if not defined PYTHON_EXE (
    if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
    echo Python launcher not found.>>"%STDERR_LOG%"
    exit /b 1
)

if not exist "%SCRIPT%" (
    if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
    echo HTTPS script not found: %SCRIPT%>>"%STDERR_LOG%"
    exit /b 1
)

netstat -ano | findstr /R /C:":%PORT% .*LISTENING" >nul 2>nul
if "%ERRORLEVEL%"=="0" (
    exit /b 0
)

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
echo ==== %date% %time% ====>>"%STDOUT_LOG%"
echo ROOT=%ROOT%>>"%STDOUT_LOG%"
echo SCRIPT=%SCRIPT%>>"%STDOUT_LOG%"
echo PYTHON=%PYTHON_EXE%>>"%STDOUT_LOG%"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$p='%PYTHON_EXE%'; $s='%SCRIPT%'; $o='%STDOUT_LOG%'; $e='%STDERR_LOG%'; Start-Process -WindowStyle Minimized -FilePath $p -ArgumentList @($s) -RedirectStandardOutput $o -RedirectStandardError $e"
exit /b 0
