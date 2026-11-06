@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "APP=%~dp0heart_overlay.pyw"
set "PYW=%~dp0venv\Scripts\pythonw.exe"
if exist "%PYW%" goto venv
where pyw >nul 2>nul
if not errorlevel 1 goto pyw
where pythonw >nul 2>nul
if not errorlevel 1 goto sys
goto nopy

:venv
start "" "%PYW%" "%APP%"
exit /b

:pyw
start "" pyw -3 "%APP%"
exit /b

:sys
start "" pythonw "%APP%"
exit /b

:nopy
echo.
echo   没有找到 Python 运行环境。
echo   请安装 Python 3.11 或更高版本（安装时勾选 Add to PATH）。
echo   或者直接用网页版：双击 启动.bat
echo.
pause
