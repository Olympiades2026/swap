@echo off
rem swap derriere le Apache de WAMP : ce script lance seulement le serveur Python, en local (127.0.0.1:8765).
rem Les navigateurs passent par Apache (voir wamp\swap-apache.conf). Compte a utiliser : administrateur des PC source ET cible.
cd /d "%~dp0"
where py >nul 2>&1
if %errorlevel%==0 (py -3 -m swap server --proxy --listen 127.0.0.1 --port 8765 & goto fin)
where python >nul 2>&1
if %errorlevel%==0 (python -m swap server --proxy --listen 127.0.0.1 --port 8765 & goto fin)
echo Python 3.9 ou plus recent est necessaire : https://www.python.org/downloads/
pause
exit /b 1
:fin
pause
