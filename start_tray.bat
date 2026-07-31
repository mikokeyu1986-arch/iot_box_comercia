@echo off
setlocal

cd /d "%~dp0"

echo ========================================
echo  IoT Box Runtime - 一键启动
echo ========================================
echo.

where python >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo [INFO] 正在查找 Python...
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
            set "PYTHON=%%~P"
            goto :python_found
        )
    )
    where py >nul 2>nul && set "PYTHON=py" && goto :python_found
    echo [ERROR] 未找到 Python！请安装 Python 3.11+
    pause
    exit /b 1
)

:python_found
echo [INFO] 使用 Python: %PYTHON%

echo.
echo [INFO] 正在安装/检查依赖...
"%PYTHON%" -m pip install pyserial pystray pillow --quiet 2>nul

echo.
echo [INFO] 正在启动系统托盘程序...
echo.
"%PYTHON%" tray_app.py

endlocal
