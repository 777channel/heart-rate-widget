@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "%~dp0venv\Scripts\python.exe" goto venv
py -3 "%~dp0heart_overlay.pyw" --scan
goto done

:venv
"%~dp0venv\Scripts\python.exe" "%~dp0heart_overlay.pyw" --scan

:done
pause
