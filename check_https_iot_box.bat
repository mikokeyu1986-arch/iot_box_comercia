@echo off
setlocal

set "PORT=8398"
echo Checking HTTPS runtime on https://127.0.0.1:%PORT%
echo.

netstat -ano | findstr /R /C:":%PORT% .*LISTENING"
if errorlevel 1 (
    echo HTTPS runtime is NOT running.
    echo.
    echo Start it with start_https_iot_box.bat and keep that window open.
    pause
    exit /b 1
)

echo.
echo HTTPS runtime is listening.
echo.
curl.exe -k -s -o NUL -w "Status: %%{http_code}\n" https://127.0.0.1:%PORT%/api/status
pause
