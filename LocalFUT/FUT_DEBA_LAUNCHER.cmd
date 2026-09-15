@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "LAUNCHER_ROOT=%~dp0"
if not exist "%LAUNCHER_ROOT%tools\futdeba_launcher.py" if exist "%~dp0LocalFUT\tools" set "LAUNCHER_ROOT=%~dp0LocalFUT\"

rem A packaged beta prepares its own runtime the first time it is started, so
rem the tester never runs a separate setup file. Only a verified setup leaves
rem the marker, so an interrupted one simply runs again.
if exist "%LAUNCHER_ROOT%SETUP_RUNTIME.cmd" if not exist "%LAUNCHER_ROOT%.runtime\setup-verified.txt" (
  call "%LAUNCHER_ROOT%SETUP_RUNTIME.cmd"
  if errorlevel 1 exit /b 1
)

set "PYTHONW="
set "PYTHON_ARGS="
if exist "%LAUNCHER_ROOT%.runtime\Scripts\pythonw.exe" set "PYTHONW=%LAUNCHER_ROOT%.runtime\Scripts\pythonw.exe"
if not defined PYTHONW where pyw.exe >nul 2>nul && ( set "PYTHONW=pyw.exe" & set "PYTHON_ARGS=-3" )
if not defined PYTHONW where pythonw.exe >nul 2>nul && set "PYTHONW=pythonw.exe"
if not defined PYTHONW (
  echo Python with Tk support was not found. Run INSTALL_PREREQUISITES.cmd first.
  pause
  exit /b 1
)

start "FUT Deba Launcher" "%PYTHONW%" %PYTHON_ARGS% "%LAUNCHER_ROOT%tools\futdeba_launcher.py" %*
exit /b 0
