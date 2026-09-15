@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title FIFA 19 Local FUT - Reset account
set "PY="
set "PY_ARGS="
if exist "%~dp0.runtime\Scripts\python.exe" set "PY=%~dp0.runtime\Scripts\python.exe"
if not defined PY where py.exe >nul 2>nul && ( set "PY=py.exe" & set "PY_ARGS=-3" )
if not defined PY ( where python.exe >nul 2>nul && set "PY=python.exe" )
if not defined PY ( echo Python was not found. & pause & exit /b 1 )
echo WARNING: close FIFA 19 and PLAY_FUT19_LOCAL.cmd first.
echo.
echo FULL ACCOUNT RESET: this creates a backup and resets the local FUT account:
echo coins, inventory, squads, packs, transfer state, objectives, matches,
echo win-draw-loss record, SBC state, and onboarding.
echo The first-time FUT setup will start again with the default coin balance.
echo.
set /p "OK=Type RESET to confirm: "
if /i not "!OK!"=="RESET" ( echo Cancelled. & pause & exit /b 0 )
"%PY%" %PY_ARGS% "%~dp0server\fut_tools.py" resetaccount
if errorlevel 1 (
  echo ERROR: the local account was not reset.
  pause
  exit /b 2
)
echo.
pause
exit /b 0
