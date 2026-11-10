@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist app.pid (
    echo Server is not running.
    ping -n 3 127.0.0.1 >nul
    exit /b 0
)
set /p PID=<app.pid
taskkill /PID %PID% /F >nul 2>nul
del app.pid >nul 2>nul
echo Server stopped.
ping -n 3 127.0.0.1 >nul
