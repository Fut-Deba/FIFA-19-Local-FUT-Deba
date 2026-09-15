@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title LocalFUT19 Private Beta - Reset RTG Account
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\..\LocalFUT\tools\reset_rtg_account.ps1"
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" echo RTG account reset stopped with error %RESULT%.
pause
exit /b %RESULT%
