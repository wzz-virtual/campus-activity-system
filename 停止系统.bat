@echo off
rem  See the comments in the start script: keep CRLF + ASCII only.
chcp 65001 >nul
cd /d "%~dp0"
title Stop Campus Activity System
".venv\Scripts\python.exe" tools\serve.py stop
echo.
echo   Press any key to close.
pause >nul
