@echo off
setlocal enabledelayedexpansion

REM --- edit these ---------------------------------------------------------
set "CONDA=C:\Users\bbcr_opr\AppData\Local\miniforge3"
set "REPO=C:\Users\bbcr_opr\BBLapps"
set "DEFAULT=xlight.yaml"
REM   ^ Enter picks this one. Blank it out ("") to make Enter quit instead.
set "DEBUG=0"
REM   ^ 1 = keep this console open and show beamview's output (use when
REM     something won't start). 0 = window closes as soon as you've picked.
REM ------------------------------------------------------------------------

set "CFGDIR=%REPO%\beamview\configs"
title beamview

if not exist "%CONDA%\Scripts\activate.bat" (
    echo ERROR: conda not found at %CONDA%
    goto :end
)
if not exist "%CFGDIR%" (
    echo ERROR: no configs directory at %CFGDIR%
    goto :end
)

echo.
echo   Which cameras?
echo.

set n=0
for %%f in ("%CFGDIR%\*.yaml") do (
    set /a n+=1
    set "lab=%%~nf"
    set "got="
    for /f "usebackq tokens=1,* delims=:" %%a in ("%%f") do (
        if not defined got if /i "%%a"=="name" (
            set "raw=%%b"
            set raw=!raw:"=!
            for /f "tokens=* delims= " %%x in ("!raw!") do set "lab=%%x"
            set "got=1"
        )
    )
    set "file[!n!]=%%~nxf"
    if /i "%%~nxf"=="%DEFAULT%" (
        echo      !n!^)  !lab!    ^<-- default
    ) else (
        echo      !n!^)  !lab!
    )
)

if %n%==0 (
    echo   ERROR: no .yaml configs found in %CFGDIR%
    goto :end
)

echo.
set "pick="
if defined DEFAULT (
    set /p "pick=  Number 1-%n%, or Enter for %DEFAULT% :  "
) else (
    set /p "pick=  Number 1-%n%, blank to quit :  "
)

if not defined pick goto :usedefault

set "valid="
for /l %%i in (1,1,%n%) do if "%pick%"=="%%i" set valid=1
if not defined valid (
    echo.
    echo   "%pick%" is not one of 1-%n%.
    goto :end
)
call set "CONFIG=%%file[%pick%]%%"
goto :launch

:usedefault
if not defined DEFAULT goto :end
set "CONFIG=%DEFAULT%"

:launch
REM Try conda's own activation, but don't depend on it: on a machine where
REM the shell was never initialised for cmd.exe it prints "libmamba Shell
REM not initialized" and does nothing. Prepending the directories activate
REM would have added is enough for beamview, and works either way.
call "%CONDA%\Scripts\activate.bat" "%CONDA%" 2>nul
set "PATH=%CONDA%;%CONDA%\Library\mingw-w64\bin;%CONDA%\Library\usr\bin;%CONDA%\Library\bin;%CONDA%\Scripts;%CONDA%\bin;%PATH%"
cd /d "%REPO%"

REM Hand off to the console-less interpreter and exit straight away, so this
REM window disappears as soon as a config is picked. pythonw.exe ships beside
REM python.exe in every CPython/conda install; fall back if it's ever absent.
REM
REM The catch: pythonw has nowhere to print, so a crash on startup is
REM SILENT. Set DEBUG=1 at the top to run the normal console interpreter
REM instead and keep the window -- that's the version to use when something
REM isn't working.
if "%DEBUG%"=="1" goto :launch_debug

set "PYW=%CONDA%\pythonw.exe"
if not exist "%PYW%" set "PYW=pythonw"
start "" "%PYW%" -m beamview.main --config configs/%CONFIG%
exit /b 0

:launch_debug
echo.
echo   Starting beamview  (%CONFIG%)   [DEBUG: console kept open]
echo.
python -m beamview.main --config configs/%CONFIG%

REM Only reached on an error, or in DEBUG -- so the pause stays. Without it
REM a bad path or a bad config number would flash past unreadably.
:end
echo.
echo   Press any key to close.
pause >nul
