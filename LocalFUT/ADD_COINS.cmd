@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title FIFA 19 Local FUT - Add coins
set "PY="
set "PY_ARGS="
if exist "%~dp0.runtime\Scripts\python.exe" set "PY=%~dp0.runtime\Scripts\python.exe"
if not defined PY where py.exe >nul 2>nul && ( set "PY=py.exe" & set "PY_ARGS=-3" )
if not defined PY ( where python.exe >nul 2>nul && set "PY=python.exe" )
if not defined PY ( echo Python was not found. Run INSTALL_PREREQUISITES.cmd first. & pause & exit /b 1 )
echo Current local account:
"%PY%" %PY_ARGS% "%~dp0server\fut_tools.py" info
if errorlevel 1 (
  echo ERROR: the local account could not be read.
  pause
  exit /b 2
)
echo.
set /p "AMT=Coins to add (Enter = 1000000): "
if not defined AMT set "AMT=1000000"
set "INVALID_AMOUNT="
for /f "delims=0123456789" %%A in ("!AMT!") do set "INVALID_AMOUNT=1"
if defined INVALID_AMOUNT (
  echo ERROR: enter a positive whole number.
  pause
  exit /b 3
)
if "!AMT!"=="0" (
  echo ERROR: enter a positive whole number.
  pause
  exit /b 3
)
"%PY%" %PY_ARGS% "%~dp0server\fut_tools.py" addcoins "!AMT!"
if errorlevel 1 (
  echo ERROR: coins were not added.
  pause
  exit /b 4
)
echo.
pause
exit /b 0
