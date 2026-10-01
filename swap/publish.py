"""Fichiers produits avec une analyse ou une sauvegarde : inventaire, rapports, script, règles, outil de restauration."""

from __future__ import annotations

import json
import os
import shutil
import tempfile

from .redirects import TEMPLATE
from .report import build_report
from .scripts import build_setup_script

# Lanceur de restauration déposé dans la sauvegarde : interface graphique si possible, sinon ligne de commande.
RESTORE_BAT = (
    "@echo off\r\n"
    'cd /d "%~dp0outil"\r\n'
    "where pyw >nul 2>&1\r\n"
    'if %errorlevel%==0 (start "" pyw -3 -m swap web --backup "%~dp0." & exit /b)\r\n'
    "where python >nul 2>&1\r\n"
    'if %errorlevel%==0 (python -m swap restore --backup "%~dp0." -i & pause & exit /b)\r\n'
    "echo Python 3.9 ou plus recent est necessaire : https://www.python.org/downloads/\r\n"
    "pause\r\n"
)

INVENTORY = "inventaire.json"
RULES_FILE = "regles.txt"


def write_outputs(inv: dict, out_dir: str, with_rules: bool = True) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    report = build_report(inv)
    paths = {
        "inventaire": os.path.join(out_dir, INVENTORY),
        "rapport_html": os.path.join(out_dir, "rapport.html"),
        "rapport_md": os.path.join(out_dir, "rapport.md"),
        "script": os.path.join(out_dir, "installer_et_configurer.ps1"),
        "regles": os.path.join(out_dir, RULES_FILE),
    }
    with open(paths["inventaire"], "w", encoding="utf-8") as fh:
        json.dump(inv, fh, ensure_ascii=False, indent=1)
    with open(paths["rapport_html"], "w", encoding="utf-8") as fh:
        fh.write(report.html())
    with open(paths["rapport_md"], "w", encoding="utf-8") as fh:
        fh.write(report.md())
    with open(paths["script"], "w", encoding="utf-8-sig", newline="") as fh:  # BOM : PowerShell 5 lit bien les accents
        fh.write(build_setup_script(inv))
    if with_rules and not os.path.exists(paths["regles"]):  # ne jamais écraser les règles déjà écrites par l'utilisateur
        with open(paths["regles"], "w", encoding="utf-8-sig", newline="") as fh:
            fh.write(TEMPLATE)
    return paths


def install_tool(backup: str) -> None:
    """Dépose l'outil et un lanceur dans la sauvegarde : le nouveau poste n'a besoin que de Python."""
    tool_dir = os.path.join(backup, "outil")
    shutil.rmtree(tool_dir, ignore_errors=True)
    shutil.copytree(os.path.dirname(os.path.abspath(__file__)), os.path.join(tool_dir, "swap"),
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    with open(os.path.join(backup, "RESTAURER.bat"), "w", encoding="cp1252", newline="") as fh:
        fh.write(RESTORE_BAT)


def publish_extras(inv: dict, sink) -> None:
    """Envoie à la destination (locale ou distante) rapports, script de reconfiguration, outil et lanceur de restauration."""
    with tempfile.TemporaryDirectory() as tmp:
        write_outputs(inv, tmp, with_rules=False)
        install_tool(tmp)
        sink.put_tree(tmp)
