@echo off
setlocal
cd /d "%~dp0"
title Daily QC Operation Report

rem ---- find Python 3.10 or newer
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY ( where python >nul 2>nul && set "PY=python" )
if not defined PY (
  echo.
  echo  [ERROR] Python was not found on this PC.
  echo  Install Python 3.10 or newer from https://www.python.org/downloads/
  echo  and tick "Add python.exe to PATH" during setup. Then run start.bat again.
  echo.
  pause
  exit /b 1
)

rem ---- first run: create the private Python environment
if not exist ".venv\Scripts\python.exe" (
  echo.
  echo  First run: preparing the program. This takes 1-3 minutes and needs internet once.
  echo.
  %PY% -m venv .venv
  if errorlevel 1 goto :fail
)

rem ---- install or update packages when requirements-local.txt has changed
fc /b "requirements-local.txt" ".venv\installed.txt" >nul 2>nul
if errorlevel 1 (
  echo  Installing packages ...
  ".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
  ".venv\Scripts\python.exe" -m pip install -r requirements-local.txt
  if errorlevel 1 goto :fail
  copy /y "requirements-local.txt" ".venv\installed.txt" >nul
)

rem ---- start the server in its own minimised window; the browser opens by itself
start "Daily QC Operation Report - server (do not close)" /min ".venv\Scripts\python.exe" run.py
echo.
echo  Daily QC Operation Report is starting. The browser will open in a few seconds.
echo  To stop the program: run stop.bat, or use "Shut down" in the program menu (Admin).
echo.
timeout /t 4 >nul
exit /b 0

:fail
echo.
echo  [ERROR] Setup did not finish. Check the internet connection and the messages above,
echo  then run start.bat again. If it keeps failing, delete the .venv folder and retry.
echo.
pause
exit /b 1
