@echo off
rem Heberge swap sur ce serveur : ouvrir ensuite http://NOM-DU-SERVEUR:8080 depuis n'importe quel poste.
rem Compte a utiliser : administrateur sur les PC source ET cible. Fenetre a laisser ouverte.
cd /d "%~dp0"
where py >nul 2>&1
if %errorlevel%==0 (py -3 -m swap server --port 8080 --firewall & goto fin)
where python >nul 2>&1
if %errorlevel%==0 (python -m swap server --port 8080 --firewall & goto fin)
echo Python 3.9 ou plus recent est necessaire : https://www.python.org/downloads/
pause
exit /b 1
:fin
pause
