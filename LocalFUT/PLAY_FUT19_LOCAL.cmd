@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title FIFA 19 Local FUT

set "DISPATCH_ARGS=%*"
if not defined DISPATCH_ARGS if defined LOCALFUT19_ELEVATED_ARGS set "DISPATCH_ARGS=%LOCALFUT19_ELEVATED_ARGS%"

rem --- Administrator rights are required for process attachment ---
rem Windows builds a fresh environment for a process elevated through the
rem UAC broker, so LOCALFUT19_ELEVATED_ARGS never reaches it. Selecting
rem the RTG account this way silently fell back to the main account and
rem both shared one database. The arguments must travel on the command
rem line of the elevated process.

fltmc >nul 2>nul
if errorlevel 1 (
  echo Requesting Administrator privileges...
  set "LOCALFUT19_ELEVATED_ARGS=%DISPATCH_ARGS%"
  set "LOCALFUT19_ELEVATE_TARGET=%~f0"
  powershell -NoProfile -ExecutionPolicy Bypass -Command "$target=$env:LOCALFUT19_ELEVATE_TARGET; $arguments=$env:LOCALFUT19_ELEVATED_ARGS; if ($arguments) { Start-Process -FilePath $target -ArgumentList $arguments -Verb RunAs } else { Start-Process -FilePath $target -Verb RunAs }"
  exit /b
)

echo ============================================================
echo  FIFA 19 LOCAL FUT - UNIFIED LAUNCHER
echo ============================================================
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\start_localfut19.ps1" %DISPATCH_ARGS%
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" echo LocalFUT19 stopped with error %RESULT%.
pause
exit /b %RESULT%
