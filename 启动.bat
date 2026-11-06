@echo off
chcp 65001 >nul
set "PAGE=%~dp0heart-rate.html"
set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
set "CHROME86=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
set "EDGE=C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
if exist "%CHROME%" goto c1
if exist "%CHROME86%" goto c2
if exist "%EDGE%" goto e1
goto def

:c1
start "" "%CHROME%" "%PAGE%"
exit /b

:c2
start "" "%CHROME86%" "%PAGE%"
exit /b

:e1
start "" "%EDGE%" "%PAGE%"
exit /b

:def
start "" "%PAGE%"
exit /b
