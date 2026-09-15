@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "ACCOUNT_MODE=%~1"
if /I "%ACCOUNT_MODE%"=="NORMAL" goto mode_ready
if /I "%ACCOUNT_MODE%"=="RTG" goto mode_ready
echo Usage: MIGRATE_V1_ACCOUNT.cmd NORMAL or RTG
echo No account database was changed.
exit /b 2

:mode_ready
set "PYTHON_EXE=%CD%\.runtime\Scripts\python.exe"
if exist "%PYTHON_EXE%" goto python_ready
for %%P in (python.exe) do set "PYTHON_EXE=%%~$PATH:P"
if defined PYTHON_EXE goto python_ready
echo Python was not found. Run setup once before migrating.
exit /b 2

:python_ready
if defined LOCALFUT19_DATA_ROOT (
    set "ACCOUNT_ROOT=%LOCALFUT19_DATA_ROOT%"
) else (
    set "ACCOUNT_ROOT=%LOCALAPPDATA%\FIFA19LocalFUT"
)
"%PYTHON_EXE%" -B "%CD%\tools\migrate_v1_account.py" --data-root "%ACCOUNT_ROOT%" --account-mode "%ACCOUNT_MODE%"
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" echo Migration failed. No existing profile was replaced.
exit /b %RESULT%
