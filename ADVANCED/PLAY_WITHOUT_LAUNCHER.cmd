@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title LocalFUT19 Private Beta - Play without the launcher
set "MODE=%~1"
if /i "%MODE%"=="RTG" (
  set "LOCALFUT19_PROFILE=RTG"
  set "LOCALFUT19_RTG_MODE=1"
  call "%~dp0..\LocalFUT\PLAY_FUT19_LOCAL.cmd" -AccountMode RTG
) else (
  call "%~dp0..\LocalFUT\PLAY_FUT19_LOCAL.cmd"
)
exit /b %errorlevel%
