@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title FIFA 19 Local FUT - Draft Quality

set "PY="
set "PY_ARGS="
if exist "%~dp0.runtime\Scripts\python.exe" set "PY=%~dp0.runtime\Scripts\python.exe"
where py.exe >nul 2>nul
if not defined PY if not errorlevel 1 ( set "PY=py.exe" & set "PY_ARGS=-3" )
if not defined PY ( where python.exe >nul 2>nul && set "PY=python.exe" )
if not defined PY (
  echo ERROR: Python was not found. Run INSTALL_PREREQUISITES.cmd first.
  pause
  exit /b 1
)

if not "%~1"=="" goto :argument

echo ============================================================
echo  FIFA 19 LOCAL FUT - DRAFT QUALITY
echo ============================================================
"%PY%" %PY_ARGS% "%~dp0server\fut_draft_config.py" show
echo.
echo  WHEN TO CHANGE IT:
echo  - Best: before starting the server and FIFA 19.
echo  - Also OK: from the FUT main menu, before creating a new Draft.
echo  - Do not change preset while a Draft is already in progress.
echo.
echo  1^) Classic - balanced original distribution
echo  2^) Boosted - more specials, about 2 high-rated cards per 5
echo  3^) Creator - all promo types, about 3 cards rated 88+ per 5
echo  4^) Insane  - five specials, about 4 cards rated 90+ per 5
echo.
set /p "CHOICE=Select preset [1-4]: "
if "%CHOICE%"=="1" set "PRESET=classic"
if "%CHOICE%"=="2" set "PRESET=boosted"
if "%CHOICE%"=="3" set "PRESET=creator"
if "%CHOICE%"=="4" set "PRESET=insane"
if not defined PRESET (
  echo Invalid selection.
  pause
  exit /b 2
)
goto :save

:argument
set "PRESET=%~1"

:save
"%PY%" %PY_ARGS% "%~dp0server\fut_draft_config.py" set "%PRESET%"
if errorlevel 1 (
  pause
  exit /b 3
)
echo.
echo IMPORTANT: the preset applies when a new Draft is created.
echo Do not switch preset during an active Draft.
pause
exit /b 0
