@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_integrated.ps1" %*
if errorlevel 1 pause
exit /b %errorlevel%
