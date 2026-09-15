@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title FIFA 19 Local FUT - Reset club
set "PY="
set "PY_ARGS="
if exist "%~dp0.runtime\Scripts\python.exe" set "PY=%~dp0.runtime\Scripts\python.exe"
if not defined PY where py.exe >nul 2>nul && ( set "PY=py.exe" & set "PY_ARGS=-3" )
if not defined PY ( where python.exe >nul 2>nul && set "PY=python.exe" )
if not defined PY ( echo Python was not found. & pause & exit /b 1 )
echo CLUB-ONLY RESET: this creates a backup, deletes inventory items and squads,
echo then restores Starter Italy and the starter inventory.
echo.
echo Coins, win-draw-loss record, unopened packs, objectives, match history,
echo transfer history, SBC progress, and account setup are NOT reset.
echo Use RESET_ACCOUNT.cmd only if you want a completely fresh FUT account.
echo.
set /p "OK=Continue? (y/N): "
if /i not "!OK!"=="y" ( echo Cancelled. & pause & exit /b 0 )
"%PY%" %PY_ARGS% "%~dp0server\fut_tools.py" reset
if errorlevel 1 (
  echo ERROR: the local club was not reset.
  pause
  exit /b 2
)
echo.
pause
exit /b 0
