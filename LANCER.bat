@echo off
chcp 65001 >nul
cd /d "%~dp0"
where pyw >nul 2>&1
if %errorlevel%==0 (start "" pyw -3 -m swap gui & exit /b)
where pythonw >nul 2>&1
if %errorlevel%==0 (start "" pythonw -m swap gui & exit /b)
echo Python 3.9 ou plus recent (avec tkinter) est necessaire : https://www.python.org/downloads/
pause
