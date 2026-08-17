@echo off
REM Where is it in stock?
REM
REM   Double-click            -> prompts for everything
REM   find.bat "grape gas"    -> nearest 10
REM   find.bat "grape gas" 25 -> nearest 25
REM   find.bat "grape gas" all           -> every store in Alberta
REM   find.bat "grape gas" all Ontario   -> every store in Ontario
REM
REM Accepting arguments also makes this testable without a human at the
REM keyboard, which prompt-only batch files are not.
REM
REM Uses goto labels rather than if/else blocks: cmd mis-parses quoted
REM assignments inside parenthesised blocks.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" goto :nosetup

set "PRODUCT=%~1"
set "HOWMANY=%~2"
set "PROV=%~3"

if not "%PRODUCT%"=="" goto :haveproduct

echo.
echo   ==========================================
echo    Canna Cabana - where is it in stock?
echo   ==========================================
echo.
set /p PRODUCT="  What are you looking for?  "
if "%PRODUCT%"=="" goto :nothing

echo.
echo   How wide should the search be?
echo     [Enter]  nearest 10 stores            ~15 seconds
echo      25      nearest 25 stores            ~40 seconds
echo      ALL     every store in the province  ~2 minutes
echo.
set /p HOWMANY="  Choice:  "

:haveproduct
if "%HOWMANY%"=="" set "HOWMANY=10"

if /i "%HOWMANY%"=="ALL" goto :allstores
if /i "%HOWMANY%"=="A"   goto :allstores

REM ---- nearest N -----------------------------------------------------------
if not "%PROV%"=="" set "WHERE=%PROV%"
if not "%WHERE%"=="" goto :runnear
if not "%~1"=="" goto :runnear_default
echo.
set /p WHERE="  Near which city/postal code? [Enter = default]  "
if "%WHERE%"=="" goto :runnear_default

:runnear
echo.
echo   Checking %HOWMANY% stores near %WHERE%...
echo.
.venv\Scripts\python.exe main.py --product "%PRODUCT%" --near "%WHERE%" --top %HOWMANY%
goto :done

:runnear_default
echo.
echo   Checking the %HOWMANY% nearest stores...
echo.
.venv\Scripts\python.exe main.py --product "%PRODUCT%" --top %HOWMANY%
goto :done

REM ---- whole province ------------------------------------------------------
:allstores
if not "%PROV%"=="" goto :runall
if not "%~1"=="" goto :runall_default
echo.
echo   Alberta 92  ^|  Ontario 100  ^|  Saskatchewan 13
echo   Manitoba 12 ^|  British Columbia 8
echo.
set /p PROV="  Which province? [Enter = Alberta]  "
if "%PROV%"=="" goto :runall_default

:runall
echo.
echo   Checking every store in %PROV%. This takes about 2 minutes.
echo.
.venv\Scripts\python.exe main.py --product "%PRODUCT%" --all --province "%PROV%"
goto :done

:runall_default
echo.
echo   Checking every Alberta store. This takes about 2 minutes.
echo.
.venv\Scripts\python.exe main.py --product "%PRODUCT%" --all
goto :done

REM ---- exits ---------------------------------------------------------------
:done
echo.
echo   ------------------------------------------
echo    Results are cached, so re-running is
echo    instant. Add --refresh to force a check.
echo   ------------------------------------------
if "%~1"=="" pause
exit /b 0

:nothing
echo   Nothing entered. Closing.
timeout /t 2 >nul
exit /b 0

:nosetup
echo.
echo   Setup has not been run yet. Open a terminal here and run:
echo.
echo     python -m venv .venv
echo     .venv\Scripts\pip install playwright beautifulsoup4 pandas
echo     .venv\Scripts\python -m playwright install chromium
echo.
pause
exit /b 1
