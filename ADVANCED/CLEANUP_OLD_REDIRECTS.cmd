@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title LocalFUT19 Private Beta - Remove old redirects

fltmc >nul 2>nul
if errorlevel 1 (
  echo Requesting Administrator privileges for the Windows hosts file...
  set "LOCALFUT19_ELEVATE_TARGET=%~f0"
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath $env:LOCALFUT19_ELEVATE_TARGET -Verb RunAs"
  exit /b
)
set "HOSTS=%WINDIR%\System32\drivers\etc\hosts"
echo Removing only legacy Local FUT localhost redirects...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\LocalFUT\tools\remove_localfut_hosts.ps1" -HostsPath "%HOSTS%"
if errorlevel 1 (
  echo ERROR: the Windows hosts file could not be updated.
  pause
  exit /b 1
)
ipconfig /flushdns >nul 2>nul
echo Cleanup complete. The launcher will manage temporary redirects automatically.
pause
exit /b 0
