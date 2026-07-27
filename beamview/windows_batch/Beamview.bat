@echo off
setlocal enabledelayedexpansion

REM --- edit these ---------------------------------------------------------
set "CONDA=C:\Users\bbcr_opr\AppData\Local\miniforge3"
set "REPO=C:\Users\bbcr_opr\BBLapps"
set "DEFAULT=xlight.yaml"
REM   ^ Enter picks this one. Blank it out ("") to make Enter quit instead.
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
call "%CONDA%\Scripts\activate.bat" "%CONDA%"
cd /d "%REPO%"
echo.
echo   Starting beamview  (%CONFIG%)
echo.
python -m beamview.main --config configs/%CONFIG%

:end
echo.
echo   Closed. Press any key.
pause >nul