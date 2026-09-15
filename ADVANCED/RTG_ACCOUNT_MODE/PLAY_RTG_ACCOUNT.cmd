@echo off
setlocal EnableExtensions
cd /d "%~dp0"
call "%~dp0..\..\FUT_DEBA_LAUNCHER.cmd" --account-mode RTG
exit /b %errorlevel%
