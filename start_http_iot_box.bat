@echo off
setlocal

set "ROOT="
set "SCRIPT="
set "PORT=8399"
set "LOG_DIR="
set "STDOUT_LOG="
set "STDERR_LOG="
set "PYTHON_EXE="
set "IOT_ESCPOS_ENCODING=gb18030"

call :resolve_root "%~dp0"
if not defined ROOT call :resolve_root "%CD%"
if not defined ROOT call :resolve_root "%CD%\custom_iot_box_runtime_native"

if not defined ROOT (
    echo Could not locate runtime root. Expected run_http.py beside this BAT or in the working directory.
    exit /b 1
)

set "SCRIPT=%ROOT%\run_http.py"
set "LOG_DIR=%ROOT%\logs"
set "STDOUT_LOG=%LOG_DIR%\http_stdout.log"
set "STDERR_LOG=%LOG_DIR%\http_stderr.log"

cd /d "%ROOT%"

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
    echo ========================================
    echo ERROR: Python not found!
    echo ========================================
    echo Please install Python 3.11 or later from https://www.python.org/downloads/
    echo.
    echo Python launcher not found.>>"%STDERR_LOG%"
    pause
    exit /b 1
)

if not exist "%SCRIPT%" (
    if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
    echo ========================================
    echo ERROR: Runtime script not found!
    echo ========================================
    echo Expected: %SCRIPT%
    echo.
    echo HTTP script not found: %SCRIPT%>>"%STDERR_LOG%"
    pause
    exit /b 1
)

netstat -ano | findstr /R /C:":%PORT% .*LISTENING" >nul 2>nul
if "%ERRORLEVEL%"=="0" (
    echo HTTP runtime is already running on http://127.0.0.1:%PORT%
    exit /b 0
)

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
break >"%STDOUT_LOG%"
break >"%STDERR_LOG%"
echo ==== %date% %time% ====>>"%STDOUT_LOG%"
echo ROOT=%ROOT%>>"%STDOUT_LOG%"
echo SCRIPT=%SCRIPT%>>"%STDOUT_LOG%"
echo PYTHON=%PYTHON_EXE%>>"%STDOUT_LOG%"

set "IOT_HTTP_PORT=%PORT%"
echo ========================================
echo  Starting HTTP runtime
echo  URL: http://127.0.0.1:%PORT%
echo ========================================
echo  Keep this window open to keep the service running.
echo  Close it to stop the HTTP service.
echo ========================================
echo.
"%PYTHON_EXE%" ".\run_http.py" 1>>"%STDOUT_LOG%" 2>>"%STDERR_LOG%"

echo.
echo ========================================
echo  HTTP runtime stopped.
echo ========================================
echo.
echo  Log files:
echo    stdout: %STDOUT_LOG%
echo    stderr: %STDERR_LOG%
echo.
if %ERRORLEVEL% NEQ 0 (
    echo  An error occurred (exit code: %ERRORLEVEL%).
    echo  Last error log:
    type "%STDERR_LOG%" 2>nul
) else (
    echo  Service exited normally.
)
echo.
pause
exit /b %ERRORLEVEL%

:resolve_root
set "CANDIDATE=%~1"
if not defined CANDIDATE goto :eof
for %%I in ("%CANDIDATE%") do set "CANDIDATE=%%~fI"
if exist "%CANDIDATE%\run_http.py" if exist "%CANDIDATE%\app" if exist "%CANDIDATE%\web" (
    set "ROOT=%CANDIDATE%"
)
goto :eof

:wait_for_port
set "START_OK="
for /l %%I in (1,1,10) do (
    netstat -ano | findstr /R /C:":%PORT% .*LISTENING" >nul 2>nul
    if not errorlevel 1 (
        set "START_OK=1"
        goto :eof
    )
    ping 127.0.0.1 -n 2 >nul
)
goto :eof
