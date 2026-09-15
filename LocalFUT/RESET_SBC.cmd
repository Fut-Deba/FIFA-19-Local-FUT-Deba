@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title FIFA 19 Local FUT - Reset SBC progress
set "PY="
set "PY_ARGS="
if exist "%~dp0.runtime\Scripts\python.exe" set "PY=%~dp0.runtime\Scripts\python.exe"
if not defined PY where py.exe >nul 2>nul && ( set "PY=py.exe" & set "PY_ARGS=-3" )
if not defined PY ( where python.exe >nul 2>nul && set "PY=python.exe" )
if not defined PY ( echo Python was not found. & pause & exit /b 1 )
echo Close FIFA 19 and let the launcher window finish before continuing.
echo.
echo This forgets which Squad Building Challenges you have completed, so a
echo set you have already finished can be played again. Cards and rewards
echo already in your club are NOT touched, and your coins are NOT changed.
echo.
echo Press ENTER without typing anything to reset every set, or type one set id
echo to reset
echo only that one (for example 19100001 for "Let's Get Started").
echo.
set "SETID="
set /p "SETID=Set id (press ENTER for all): "
set /p "OK=Continue? (y/N): "
if /i not "!OK!"=="y" ( echo Cancelled. & pause & exit /b 0 )
if defined SETID (
  "%PY%" %PY_ARGS% "%~dp0server\fut_tools.py" resetsbc !SETID!
) else (
  "%PY%" %PY_ARGS% "%~dp0server\fut_tools.py" resetsbc
)
if errorlevel 1 (
  echo ERROR: the SBC progress was not reset.
  pause
  exit /b 2
)
echo.
pause
exit /b 0
