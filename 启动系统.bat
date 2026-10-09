@echo off
rem ---------------------------------------------------------------
rem  Double-click this file to start the local dev server.
rem  It must keep CRLF line endings and stay ASCII-only:
rem  cmd.exe mis-parses batch files that contain multi-byte characters
rem  under chcp 65001, and the failure is line-dependent (hard to spot).
rem  All Chinese messages are printed by tools/serve.py instead.
rem ---------------------------------------------------------------
chcp 65001 >nul
cd /d "%~dp0"
title Campus Activity System
".venv\Scripts\python.exe" tools\serve.py start
if errorlevel 1 goto failed
start "" http://127.0.0.1:5000
ping -n 6 127.0.0.1 >nul
exit /b 0
:failed
echo.
echo   [ FAILED ] Please screenshot the messages above.
echo   Press any key to close.
pause >nul
exit /b 1
