@echo off
chcp 65001 >nul
cd /d "%~dp0"
title IoT Box - Desktop Manager

echo.
echo ========================================
echo    IoT Box Desktop - 桌面管理工具
echo ========================================
echo.

REM 检查 Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Python，请先安装 Python 3.10+
    pause
    exit /b 1
)

REM 检查依赖
python -c "import pystray" >nul 2>&1
if errorlevel 1 (
    echo [提示] pystray 未安装，将使用窗口模式运行
    echo        安装完整托盘功能: pip install pystray pillow
    echo.
)

REM 启动 GUI
echo [启动] 正在启动 IoT Box 桌面管理工具...
python gui_app.py

pause
