@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0kraken.ps1" %*
exit /b %errorlevel%
