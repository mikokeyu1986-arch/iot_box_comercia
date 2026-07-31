@echo off
setlocal

set "ROOT=%~dp0"
set "SCRIPT=%ROOT%run_https.py"
set "LOG_DIR=%ROOT%logs"
set "STDOUT_LOG=%LOG_DIR%\https_stdout.log"
set "STDERR_LOG=%LOG_DIR%\https_stderr.log"
set "PYTHON_EXE="

cd /d "%ROOT%"

if exist "C:\Users\Miko win\AppData\Local\Programs\Python\Python311\python.exe" (
    set "PYTHON_EXE=C:\Users\Miko win\AppData\Local\Programs\Python\Python311\python.exe"
)
if not defined PYTHON_EXE (
    where py >nul 2>nul && set "PYTHON_EXE=py"
)
if not defined PYTHON_EXE (
    where python >nul 2>nul && set "PYTHON_EXE=python"
)

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

echo ==== %date% %time% ====>>"%STDOUT_LOG%"
echo ==== %date% %time% ====>>"%STDERR_LOG%"
echo ROOT=%ROOT%>>"%STDOUT_LOG%"
echo SCRIPT=%SCRIPT%>>"%STDOUT_LOG%"
echo PYTHON=%PYTHON_EXE%>>"%STDOUT_LOG%"

if not defined PYTHON_EXE (
    echo Python launcher not found.>>"%STDERR_LOG%"
    echo Python launcher not found.
    pause
    exit /b 1
)

if not exist "%SCRIPT%" (
    echo HTTPS script not found: %SCRIPT%>>"%STDERR_LOG%"
    echo HTTPS script not found: %SCRIPT%
    pause
    exit /b 1
)

"%PYTHON_EXE%" "%SCRIPT%" 1>>"%STDOUT_LOG%" 2>>"%STDERR_LOG%"
echo.
echo ExitCode=%ERRORLEVEL%
pause
