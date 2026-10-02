@echo off
setlocal
cd /d "%~dp0"
title Stop Daily QC Operation Report

rem ---- same port as the program: 8790 unless PORT is set in .env
set "PORT=8790"
if exist ".env" (
  for /f "usebackq tokens=1,* delims==" %%a in (".env") do (
    if /i "%%a"=="PORT" set "PORT=%%b"
  )
)

set "FOUND="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:":%PORT% .*LISTENING"') do (
  taskkill /PID %%p /F >nul 2>nul
  set "FOUND=1"
)

echo.
if defined FOUND (
  echo  Daily QC Operation Report has been stopped. Data is saved in the data folder.
) else (
  echo  The program was not running on port %PORT%.
)
echo.
timeout /t 3 >nul
exit /b 0
