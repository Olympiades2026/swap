@echo off
chcp 65001 >nul
cd /d "%~dp0"
where pyw >nul 2>&1
if %errorlevel%==0 (start "" pyw -3 -m swap web & exit /b)
where pythonw >nul 2>&1
if %errorlevel%==0 (start "" pythonw -m swap web & exit /b)
echo Python 3.9 ou plus recent est necessaire : https://www.python.org/downloads/
pause
