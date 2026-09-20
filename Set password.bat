@echo off
REM Change the admin password without touching a command line.
REM Double-click this. It prompts, sets the password, and exits.
REM
REM Safe to run while the app is running: the server re-reads the password
REM file on every login attempt, so the new one works immediately.

cd /d "%~dp0"

if not exist "CannaCabana.exe" (
    echo.
    echo   CannaCabana.exe is not in this folder.
    echo   Keep this file next to the app.
    echo.
    pause
    exit /b 1
)

CannaCabana.exe --set-password

echo.
pause
