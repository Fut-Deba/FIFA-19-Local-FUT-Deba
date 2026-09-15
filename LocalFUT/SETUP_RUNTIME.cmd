@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title FUT Deba - First-time setup

echo ============================================================
echo  FUT DEBA - FIRST-TIME SETUP
echo ============================================================
echo.
echo FUT_DEBA_LAUNCHER.cmd runs this once, before the launcher first opens.
echo.

rem Windows tags every file that arrived from the internet, and Smart App
rem Control refuses to run a tagged script regardless of its contents. Clearing
rem the tag across the whole package here keeps Windows from interrupting the
rem tester on every later file. It changes no Windows setting and turns
rem nothing off.
echo Preparing the package files...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-ChildItem -LiteralPath '%~dp0..' -Recurse -Force -File -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue" >nul 2>nul

set "PY="
set "PY_ARGS="
where py.exe >nul 2>nul
if not errorlevel 1 call :try_python "py.exe" "-3"
if not defined PY (
  where python.exe >nul 2>nul
  if not errorlevel 1 call :try_python "python.exe" ""
)
if not defined PY (
  where winget.exe >nul 2>nul
  if errorlevel 1 goto :no_python
  echo Python 3.10 or newer was not found.
  echo Installing Python 3.12 for the current Windows user...
  winget install --id Python.Python.3.12 -e --source winget --scope user --silent --accept-package-agreements --accept-source-agreements
  if errorlevel 1 goto :no_python
  if exist "%LocalAppData%\Programs\Python\Python312\python.exe" call :try_python "%LocalAppData%\Programs\Python\Python312\python.exe" ""
  if not defined PY (
    where py.exe >nul 2>nul
    if not errorlevel 1 call :try_python "py.exe" "-3.12"
  )
)
if not defined PY goto :no_python

set "RUNTIME=%~dp0.runtime"
set "RUNTIME_PY=%RUNTIME%\Scripts\python.exe"
set "PACKAGE_ROOT=%~dp0.."
echo [1/4] Python found:
"%PY%" %PY_ARGS% --version
echo [2/4] Creating the isolated Local FUT runtime...
if not exist "%RUNTIME_PY%" "%PY%" %PY_ARGS% -m venv "%RUNTIME%"
if not exist "%RUNTIME_PY%" (
  echo ERROR: the isolated Python runtime could not be created.
  goto :blocked
)

echo [3/4] Installing the beta dependencies...
"%RUNTIME_PY%" -m pip install --upgrade pip --disable-pip-version-check
"%RUNTIME_PY%" -m pip install -r "%~dp0requirements-beta.txt"
if errorlevel 1 (
  echo ERROR: dependency installation failed. Check the Internet connection.
  goto :blocked
)
"%RUNTIME_PY%" -c "import frida, cryptography, tkinter; print('frida', frida.__version__); print('cryptography', cryptography.__version__)"
if errorlevel 1 (
  echo ERROR: dependency verification failed.
  goto :blocked
)

rem The launcher finds and verifies the FIFA 19 installation on its Game page,
rem with automatic detection and a folder browser, so setup verifies only the
rem package it has just prepared.
echo [4/4] Verifying the package files...
"%RUNTIME_PY%" "%~dp0tools\verify_private_beta.py" --package "%PACKAGE_ROOT%" --package-only
if errorlevel 1 goto :blocked

rem Only a verified setup is remembered; anything else runs again next start.
>"%RUNTIME%\setup-verified.txt" echo verified
echo.
echo SETUP VERIFIED. The launcher opens now.
exit /b 0

:blocked
echo.
echo SETUP BLOCKED. Read the errors above, then start FUT_DEBA_LAUNCHER.cmd again.
pause
exit /b 6

:no_python
echo.
echo ERROR: Python 3.10+ was not found and automatic installation failed.
echo Install a 64-bit Python from https://www.python.org, then start FUT_DEBA_LAUNCHER.cmd again.
pause
exit /b 2

:try_python
"%~1" %~2 -c "import struct,sys; raise SystemExit(0 if sys.version_info >= (3,10) and struct.calcsize('P') == 8 else 1)" >nul 2>nul
if errorlevel 1 exit /b 0
set "PY=%~1"
set "PY_ARGS=%~2"
exit /b 0
