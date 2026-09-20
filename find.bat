@echo off
REM Where is it in stock?
REM
REM   Double-click  -> keeps asking until you quit
REM
REM   find.bat "grape gas"                  -> nearest 10, runs once and exits
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
REM Argument mode runs once and exits so it stays scriptable, and testable
REM without a human at the keyboard. Interactive mode loops.
REM
REM Uses goto labels rather than if/else blocks: cmd mis-parses quoted
REM assignments inside parenthesised blocks.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" goto :nosetup

REM ---- argument mode: one shot ---------------------------------------------
if "%~1"=="" goto :interactive

set "PRODUCT=%~1"
set "HOWMANY=%~2"
set "PROV=%~3"
set "SRC="
set "WHERE="
call :readsrc "%~2"
call :readsrc "%~3"
call :readsrc "%~4"
call :clearsrc HOWMANY "%HOWMANY%"
call :clearsrc PROV "%PROV%"
call :runsearch
exit /b 0

REM ---- interactive mode: loop ----------------------------------------------
:interactive
echo.
echo   ==========================================
echo    Canna Cabana - where is it in stock?
echo   ==========================================

:mainloop
REM Every prompt is re-read each pass, so stale answers never leak between
REM searches -- that was the bug that made this single-use.
set "PRODUCT="
set "HOWMANY="
set "PROV="
set "WHERE="
set "SRC="
set "SRCPICK="
set "AGAIN="

echo.
set /p PRODUCT="  What are you looking for?  (Q to quit)  "
if "%PRODUCT%"=="" goto :bye
if /i "%PRODUCT%"=="Q" goto :bye
if /i "%PRODUCT%"=="QUIT" goto :bye
if /i "%PRODUCT%"=="EXIT" goto :bye

echo.
echo   How wide?   [Enter] nearest 10    25 = nearest 25    ALL = whole province
set /p HOWMANY="  Choice:  "

echo.
echo   Source?     [Enter] Index (instant)    L = Live (now)    W = Web (browser)
set /p SRCPICK="  Choice:  "
if /i "%SRCPICK%"=="L" set "SRC=live"
if /i "%SRCPICK%"=="LIVE" set "SRC=live"
if /i "%SRCPICK%"=="W" set "SRC=web"
if /i "%SRCPICK%"=="WEB" set "SRC=web"
if "%SRC%"=="" set "SRC=index"

if "%HOWMANY%"=="" set "HOWMANY=10"
if /i "%HOWMANY%"=="ALL" goto :ask_prov
if /i "%HOWMANY%"=="A" goto :ask_prov

echo.
set /p WHERE="  Near which city/postal code? [Enter = default]  "
goto :go

:ask_prov
echo.
echo   Alberta 92  ^|  Ontario 100  ^|  Saskatchewan 13  ^|  Manitoba 12  ^|  BC 8
set /p PROV="  Which province? [Enter = Alberta]  "

:go
echo.
call :runsearch

echo.
echo   ------------------------------------------
set /p AGAIN="  Another search? [Enter = yes, Q = quit]  "
if /i "%AGAIN%"=="Q" goto :bye
if /i "%AGAIN%"=="QUIT" goto :bye
if /i "%AGAIN%"=="N" goto :bye
goto :mainloop

REM ---- the actual search ---------------------------------------------------
:runsearch
if "%HOWMANY%"=="" set "HOWMANY=10"
if "%SRC%"=="" set "SRC=index"

set "SRCFLAGS=--cached"
if /i "%SRC%"=="live" set "SRCFLAGS=--fetcher api --refresh"
if /i "%SRC%"=="web"  set "SRCFLAGS=--fetcher browser --refresh"

if /i "%HOWMANY%"=="ALL" goto :rs_all
if /i "%HOWMANY%"=="A"   goto :rs_all

REM For nearest-N, a third argument means "near here".
if "%WHERE%"=="" if not "%PROV%"=="" set "WHERE=%PROV%"
if "%WHERE%"=="" goto :rs_near_default
.venv\Scripts\python.exe main.py --product "%PRODUCT%" --near "%WHERE%" --top %HOWMANY% %SRCFLAGS%
exit /b 0

:rs_near_default
.venv\Scripts\python.exe main.py --product "%PRODUCT%" --top %HOWMANY% %SRCFLAGS%
exit /b 0

:rs_all
if "%PROV%"=="" goto :rs_all_default
.venv\Scripts\python.exe main.py --product "%PRODUCT%" --all --province "%PROV%" %SRCFLAGS%
exit /b 0

:rs_all_default
.venv\Scripts\python.exe main.py --product "%PRODUCT%" --all %SRCFLAGS%
exit /b 0

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
:bye
echo.
echo   Bye.
timeout /t 1 >nul
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
