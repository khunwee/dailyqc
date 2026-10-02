@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo  Run start.bat once first.
  pause
  exit /b 1
)
echo  This sets the password of the account "admin" back to admin1234.
pause
".venv\Scripts\python.exe" reset_admin.py
pause
