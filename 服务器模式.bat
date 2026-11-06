@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PYCMD="
where py >nul 2>nul
if not errorlevel 1 set "PYCMD=py -3"
if not defined PYCMD where python >nul 2>nul
if not defined PYCMD if not errorlevel 1 set "PYCMD=python"
if not defined PYCMD goto nopy
start "心率服务" /min %PYCMD% -m http.server 8765 --bind 127.0.0.1
timeout /t 2 >nul
start "" "http://127.0.0.1:8765/heart-rate.html"
exit /b

:nopy
start "" "%~dp0heart-rate.html"
exit /b
