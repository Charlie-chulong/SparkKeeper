@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0update.ps1" %*
set "result=%errorlevel%"
if "%~1"=="" pause
exit /b %result%
