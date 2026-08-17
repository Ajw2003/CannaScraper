@echo off
REM Double-click this to look up stock. No terminal knowledge required.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo.
  echo   Setup has not been run yet.
  echo   Open a terminal here and run:
  echo.
  echo     python -m venv .venv
  echo     .venv\Scripts\pip install playwright beautifulsoup4 pandas
  echo     .venv\Scripts\python -m playwright install chromium
  echo.
  pause
  exit /b 1
)

echo.
echo   ==========================================
echo    Canna Cabana - where is it in stock?
echo   ==========================================
echo.

set "PRODUCT="
set /p PRODUCT="  What are you looking for?  "
if "%PRODUCT%"=="" (
  echo   Nothing entered. Closing.
  timeout /t 2 >nul
  exit /b 0
)

set "WHERE="
set /p WHERE="  Near which city/postal code? [Enter = default]  "

set "HOWMANY="
set /p HOWMANY="  How many nearby stores to check? [Enter = 10]  "
if "%HOWMANY%"=="" set "HOWMANY=10"

echo.
echo   Checking... this takes about %HOWMANY% x 8 seconds.
echo.

if "%WHERE%"=="" (
  .venv\Scripts\python.exe main.py --product "%PRODUCT%" --top %HOWMANY%
) else (
  .venv\Scripts\python.exe main.py --product "%PRODUCT%" --near "%WHERE%" --top %HOWMANY%
)

echo.
pause
