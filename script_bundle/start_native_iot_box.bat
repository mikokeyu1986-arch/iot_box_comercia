@echo off
setlocal

set "ROOT=%~dp0"
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
set "PORT=8398"
set "APP_TARGET=app.main:app"
set "LOG_DIR=%ROOT%logs"
set "STDOUT_LOG=%LOG_DIR%\native_iot_stdout.log"
set "STDERR_LOG=%LOG_DIR%\native_iot_stderr.log"

cd /d "%ROOT%"

if not defined PYTHON_EXE (
    echo Python launcher not found.
    exit /b 1
)
if not exist "%PYTHON_EXE%" (
    if "%PYTHON_EXE%"=="py" goto :skip_exe_check
    if "%PYTHON_EXE%"=="python" goto :skip_exe_check
    echo Python not found: %PYTHON_EXE%
    exit /b 1
)
:skip_exe_check

netstat -ano | findstr /R /C:":%PORT% .*LISTENING" >nul 2>nul
if "%ERRORLEVEL%"=="0" (
    exit /b 0
)

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
break >"%STDOUT_LOG%"
break >"%STDERR_LOG%"
echo ==== %date% %time% ====>>"%STDOUT_LOG%"
echo ROOT=%ROOT%>>"%STDOUT_LOG%"
echo PYTHON=%PYTHON_EXE%>>"%STDOUT_LOG%"
echo APP_TARGET=%APP_TARGET%>>"%STDOUT_LOG%"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$env:IOT_VERBOSE_PERF_LOGS='1'; Start-Process -WorkingDirectory '%ROOT%' -FilePath '%PYTHON_EXE%' -ArgumentList @('-m','uvicorn','%APP_TARGET%','--host','0.0.0.0','--port','%PORT%') -RedirectStandardOutput '%STDOUT_LOG%' -RedirectStandardError '%STDERR_LOG%' -WindowStyle Hidden"
exit /b 0
