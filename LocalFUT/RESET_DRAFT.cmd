@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title FIFA 19 Local FUT - Discard the current Draft
set "PY="
set "PY_ARGS="
if exist "%~dp0.runtime\Scripts\python.exe" set "PY=%~dp0.runtime\Scripts\python.exe"
if not defined PY where py.exe >nul 2>nul && ( set "PY=py.exe" & set "PY_ARGS=-3" )
if not defined PY ( where python.exe >nul 2>nul && set "PY=python.exe" )
if not defined PY ( echo Python was not found. & pause & exit /b 1 )
echo Close FIFA 19 and let the launcher window finish before continuing.
echo.
echo A Draft cannot be left from inside the game without playing all four
echo matches. This discards the unfinished Single Player Draft and refunds
echo the entry you paid for it, so you can start a new one immediately.
echo.
echo Your club, coins and every other mode are NOT touched. A finished Draft
echo whose rewards you already claimed is kept as history.
echo.
set /p "OK=Discard the current Draft? (y/N): "
if /i not "!OK!"=="y" ( echo Cancelled. & pause & exit /b 0 )
"%PY%" %PY_ARGS% "%~dp0server\fut_tools.py" resetdraft
if errorlevel 1 (
  echo ERROR: the Draft was not discarded.
  pause
  exit /b 2
)
echo.
pause
exit /b 0
