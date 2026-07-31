@echo off
setlocal

set "ROOT=%~dp0"
set "IOT_HTTP_PORT=8399"
set "IOT_ESCPOS_ENCODING=gb18030"
set "PYTHON_EXE="

cd /d "%ROOT%"
if not exist "%ROOT%logs" mkdir "%ROOT%logs"

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
    echo Python launcher not found.>>"%ROOT%logs\http_forever_stderr.log"
    exit /b 1
)

echo ==== %date% %time% ====>>"%ROOT%logs\http_forever_stdout.log"
echo ROOT=%ROOT%>>"%ROOT%logs\http_forever_stdout.log"
echo PYTHON=%PYTHON_EXE%>>"%ROOT%logs\http_forever_stdout.log"
"%PYTHON_EXE%" ".\run_http.py" 1>>"%ROOT%logs\http_forever_stdout.log" 2>>"%ROOT%logs\http_forever_stderr.log"
echo ExitCode=%ERRORLEVEL%>>"%ROOT%logs\http_forever_stdout.log"
