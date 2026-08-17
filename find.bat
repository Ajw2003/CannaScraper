@echo off
REM Where is it in stock?
REM
REM   Double-click                          -> prompts for everything
REM   find.bat "grape gas"                  -> nearest 10, default source
REM   find.bat "grape gas" 25               -> nearest 25
REM   find.bat "grape gas" 25 Calgary       -> nearest 25 to Calgary
REM   find.bat "grape gas" all              -> every Alberta store
REM   find.bat "grape gas" all Ontario      -> every Ontario store
REM
REM Source (add as the LAST argument):
REM   index    instant, from the nightly stock index
REM   live     check stores now via the fast API      (~12s for 10 stores)
REM   web      real browser, slowest but independent  (~85s for 10 stores)
REM
REM   find.bat "grape gas" 10 Calgary live
REM   find.bat "grape gas" all "" index
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
set "SRC="

REM The source keyword may arrive in any trailing slot.
call :readsrc "%~2"
call :readsrc "%~3"
call :readsrc "%~4"

REM ...and must not then be mistaken for a width or a province.
call :clearsrc HOWMANY "%HOWMANY%"
call :clearsrc PROV "%PROV%"

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
echo     [Enter]  nearest 10 stores
echo      25      nearest 25 stores
echo      ALL     every store in the province
echo.
set /p HOWMANY="  Choice:  "

echo.
echo   Where should the answer come from?
echo     [Enter]  Index - instant, from the nightly stock index
echo      L       Live  - check the stores right now  (fast API)
echo      W       Web   - real browser fallback       (slow, independent)
echo.
set /p SRCPICK="  Choice:  "
if /i "%SRCPICK%"=="L" set "SRC=live"
if /i "%SRCPICK%"=="LIVE" set "SRC=live"
if /i "%SRCPICK%"=="W" set "SRC=web"
if /i "%SRCPICK%"=="WEB" set "SRC=web"
if "%SRC%"=="" set "SRC=index"

:haveproduct
if "%HOWMANY%"=="" set "HOWMANY=10"
if "%SRC%"=="" set "SRC=index"

REM Translate the source into flags.
set "SRCFLAGS=--cached"
if /i "%SRC%"=="live" set "SRCFLAGS=--fetcher api --refresh"
if /i "%SRC%"=="web"  set "SRCFLAGS=--fetcher browser --refresh"

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
.venv\Scripts\python.exe main.py --product "%PRODUCT%" --near "%WHERE%" --top %HOWMANY% %SRCFLAGS%
goto :done

:runnear_default
echo.
.venv\Scripts\python.exe main.py --product "%PRODUCT%" --top %HOWMANY% %SRCFLAGS%
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
.venv\Scripts\python.exe main.py --product "%PRODUCT%" --all --province "%PROV%" %SRCFLAGS%
goto :done

:runall_default
echo.
.venv\Scripts\python.exe main.py --product "%PRODUCT%" --all %SRCFLAGS%
goto :done

REM ---- helpers -------------------------------------------------------------
:readsrc
if /i "%~1"=="index"   set "SRC=index"
if /i "%~1"=="i"       set "SRC=index"
if /i "%~1"=="live"    set "SRC=live"
if /i "%~1"=="l"       set "SRC=live"
if /i "%~1"=="web"     set "SRC=web"
if /i "%~1"=="w"       set "SRC=web"
if /i "%~1"=="browser" set "SRC=web"
if /i "%~1"=="refresh" set "SRC=live"
if /i "%~1"=="r"       set "SRC=live"
exit /b 0

:clearsrc
if /i "%~2"=="index"   set "%~1="
if /i "%~2"=="i"       set "%~1="
if /i "%~2"=="live"    set "%~1="
if /i "%~2"=="l"       set "%~1="
if /i "%~2"=="web"     set "%~1="
if /i "%~2"=="w"       set "%~1="
if /i "%~2"=="browser" set "%~1="
if /i "%~2"=="refresh" set "%~1="
if /i "%~2"=="r"       set "%~1="
exit /b 0

REM ---- exits ---------------------------------------------------------------
:done
echo.
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
