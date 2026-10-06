@echo off
cd /d "%~dp0"
where py >nul 2>&1
if %errorlevel%==0 (py -3 -m swap web & goto fin)
where python >nul 2>&1
if %errorlevel%==0 (python -m swap web & goto fin)
echo Python 3.9 ou plus recent est necessaire : https://www.python.org/downloads/
pause
exit /b 1
:fin
if errorlevel 1 pause
