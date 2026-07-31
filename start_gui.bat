@echo off
REM Use the hidden launcher directly. Do not use py.exe, which creates a console process.
wscript.exe "%~dp0start_gui_hidden.vbs"
exit /b 0

chcp 65001 >nul
cd /d "%~dp0"
title IoT Box - Desktop Manager

echo.
echo ========================================
echo    IoT Box Desktop - 桌面管理工具
echo ========================================
echo.

REM ============================================================
REM 优先查找 pythonw.exe（无控制台模式，不弹黑框）
REM pythonw.exe 无法写 stdout/stderr，所以先在 python.exe 里做环境检查，
REM 检查通过后再用 pythonw.exe 启动 GUI，彻底消除 CMD 窗口残留。
REM ============================================================

set "PYEXE="
set "PWEXE="

REM 优先使用 Python Launcher（py）解析版本
where py >nul 2>nul
if %ERRORLEVEL% equ 0 (
    for /f "delims=" %%I in ('py -3 -c "import sys,os;print(sys.executable)" 2^>nul') do set "PYEXE=%%I"
    if defined PYEXE (
        set "PWEXE=%PYEXE:python.exe=pythonw.exe%"
        if not exist "%PWEXE%" set "PWEXE=%PYEXE%"
        goto :checks
    )
)

REM 回退：遍历常见安装路径
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
    if exist %%~P (
        set "PYEXE=%%~P"
        set "PWEXE=%%~dpPpythonw.exe"
        if not exist "!PWEXE!" set "PWEXE=%%~P"
        goto :checks
    )
)

REM 回退：用 PATH 上的 python
where python >nul 2>nul
if %ERRORLEVEL% equ 0 (
    for /f "delims=" %%I in ('where python') do (
        set "PYEXE=%%I"
        set "PWEXE=%%~dpIpythonw.exe"
        if not exist "!PWEXE!" set "PWEXE=%%I"
        goto :checks
    )
)

echo [错误] 未找到 Python，请先安装 Python 3.11+
pause
exit /b 1

:checks
REM 检查依赖（用 python.exe，pythonw 里弹不出提示）
"%PYEXE%" -c "import pystray" >nul 2>&1
if errorlevel 1 (
    echo [提示] pystray 未安装，将使用窗口模式运行
    echo        安装完整托盘功能: pip install pystray pillow
    echo.
)

REM 启动 GUI：用 pythonw.exe 无黑框，且 start "" /b 立刻返回，不让 CMD 挂起
echo [启动] 正在启动 IoT Box 桌面管理工具...
start "" "%PWEXE%" "%~dp0gui_app.py"

REM 立刻退出本批处理，不等待 GUI 结束，不留 CMD 窗口
exit /b 0
