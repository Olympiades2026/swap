"""Ligne de commande : analyser, copier, vérifier, restaurer."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

from . import __version__, transfer
from .locations import Locations
from .model import Item
from .report import build_report
from .scan import run_scan
from .scripts import build_setup_script
from .util import human_size

DEFAULT_OUT = "swap-sortie"
INVENTORY = "inventaire.json"


def _progress(msg: str) -> None:
    if sys.stderr.isatty():
        cols = shutil.get_terminal_size((100, 20)).columns - 1
        sys.stderr.write("\r" + msg[:cols].ljust(cols))
        sys.stderr.flush()


def _end_progress() -> None:
    if sys.stderr.isatty():
        sys.stderr.write("\r" + " " * (shutil.get_terminal_size((100, 20)).columns - 1) + "\r")


def write_outputs(inv: dict, out_dir: str) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    report = build_report(inv)
    paths = {
        "inventaire": os.path.join(out_dir, INVENTORY),
        "rapport_html": os.path.join(out_dir, "rapport.html"),
        "rapport_md": os.path.join(out_dir, "rapport.md"),
        "script": os.path.join(out_dir, "installer_et_configurer.ps1"),
    }
    with open(paths["inventaire"], "w", encoding="utf-8") as fh:
        json.dump(inv, fh, ensure_ascii=False, indent=1)
    with open(paths["rapport_html"], "w", encoding="utf-8") as fh:
        fh.write(report.html())
    with open(paths["rapport_md"], "w", encoding="utf-8") as fh:
        fh.write(report.md())
    with open(paths["script"], "w", encoding="utf-8-sig", newline="") as fh:  # BOM : PowerShell 5 lit bien les accents
        fh.write(build_setup_script(inv))
    return paths


def cmd_scan(args) -> int:
    print("Analyse du poste (peut durer quelques minutes selon la quantité de fichiers)...")
    inv = run_scan(progress=_progress, with_system=not args.no_system)
    _end_progress()
    paths = write_outputs(inv, args.out)
    items = inv["items"]
    default = [i for i in items if i["default"]]
    print(f"\n{len(items)} élément(s) trouvé(s), {len(default)} proposé(s) par défaut = {human_size(sum(i['size'] for i in default))}")
    print(f"{len([a for a in inv['apps'] if not a.get('component')])} application(s) installée(s)")
    crit = [a for a in inv["advice"] if a["level"] != "info"]
    if crit:
        print("\nPoints d'attention :")
        for a in crit:
            print(f"  - {a['text']}")
    print(f"\nRapport détaillé : {paths['rapport_html']}")
    print(f"Script de reconfiguration (à lancer sur le nouveau poste) : {paths['script']}")
    return 0


def _load_or_scan(args) -> dict:
    path = args.inventory or os.path.join(args.out, INVENTORY)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    print("Aucun inventaire trouvé : analyse du poste d'abord...")
    inv = run_scan(progress=_progress)
    _end_progress()
    write_outputs(inv, args.out)
    return inv


def _install_tool(backup: str) -> None:
    """Dépose l'outil et un lanceur dans la sauvegarde : le nouveau poste n'a besoin que de Python."""
    tool_dir = os.path.join(backup, "outil")
    shutil.rmtree(tool_dir, ignore_errors=True)
    shutil.copytree(os.path.dirname(os.path.abspath(__file__)), os.path.join(tool_dir, "swap"),
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    with open(os.path.join(backup, "RESTAURER.bat"), "w", encoding="cp1252", newline="") as fh:
        fh.write('@echo off\r\ncd /d "%~dp0outil"\r\nwhere py >nul 2>&1\r\n'
                 'if %errorlevel%==0 (py -3 -m swap restore --backup "%~dp0." -i) else (python -m swap restore --backup "%~dp0." -i)\r\n'
                 "pause\r\n")


def cmd_copy(args) -> int:
    inv = _load_or_scan(args)
    items = [Item.from_dict(d) for d in inv["items"]]
    chosen = transfer.select_items(items, args.only or (), args.skip or (), args.interactive)
    if not chosen:
        print("Rien à copier.")
        return 1
    need, free = transfer.check_space(chosen, args.dest)
    print(f"\n{len(chosen)} élément(s) à copier, {human_size(need)} → {args.dest}")
    if 0 <= free < need:
        print(f"ATTENTION : seulement {human_size(free)} libres sur la destination.")
        if not args.yes and not args.dry_run and input("Continuer quand même ? [o/N] ").strip().lower() not in ("o", "oui", "y"):
            return 1
    if any(i.sensitive for i in chosen):
        print("ATTENTION : la copie contient des données sensibles (🔒) : utilisez un support chiffré.")
    if not args.dry_run and not args.yes and input("Lancer la copie ? [O/n] ").strip().lower() in ("n", "non"):
        return 1
    summary = transfer.run_backup(chosen, inv, args.dest, dry_run=args.dry_run, want_hash=args.hash, progress=_progress)
    _end_progress()
    for item in chosen:
        r = summary["items"][item.id]
        print(f"  {item.label:55} {r.get('copied', 0):>7} copiés, {r.get('unchanged', 0):>7} déjà à jour"
              + (f", {len(r['errors'])} erreur(s)" if r["errors"] else ""))
    if args.dry_run:
        print("\n(simulation : rien n'a été écrit)")
        return 0
    write_outputs(inv, summary["backup"])
    _install_tool(summary["backup"])
    print(f"\nSauvegarde : {summary['backup']}")
    if summary["errors"]:
        print(f"{len(summary['errors'])} fichier(s) non copié(s) (verrouillés ou inaccessibles) : voir erreurs.log")
        print("Astuce : fermez Outlook/les applications concernées et relancez la même commande, seuls les fichiers manquants seront recopiés.")
    print("Sur le nouveau poste : lancez RESTAURER.bat depuis ce dossier.")
    return 0 if not summary["errors"] else 2


def cmd_verify(args) -> int:
    res = transfer.verify_backup(args.backup, deep=args.deep, progress=_progress)
    _end_progress()
    print(f"{res['checked']} fichier(s) vérifié(s)" + (" (empreintes comprises)" if args.deep and res["hashed"] else ""))
    if args.deep and not res["hashed"]:
        print("Cette sauvegarde n'a pas d'empreintes : refaire la copie avec --hash pour une vérification du contenu.")
    for p in res["problems"][:50]:
        print("  !", p)
    if res["problems"]:
        print(f"{len(res['problems'])} problème(s).")
        return 2
    print("Sauvegarde conforme.")
    return 0


def cmd_restore(args) -> int:
    loc = Locations.detect()
    if not args.dry_run:
        print("Fermez les applications concernées (Outlook, navigateurs, VS Code...) avant de restaurer leur configuration.")
    res = transfer.run_restore(args.backup, loc, only=args.only or (), skip=args.skip or (), overwrite=args.overwrite,
                               dry_run=args.dry_run, interactive=args.interactive)
    for item_id, r in res["items"].items():
        print(f"  {item_id:45} {r['restored']:>7} restauré(s), {r['skipped']:>7} déjà présent(s)"
              + (f", {len(r['errors'])} erreur(s)" if r["errors"] else ""))
    for e in res["errors"][:20]:
        print("  !", e)
    script = os.path.join(args.backup, "installer_et_configurer.ps1")
    if os.path.exists(script):
        print(f"\nÉtape suivante : relisez puis lancez {script} dans PowerShell (lecteurs réseau, imprimantes, applications).")
    print("Puis consultez rapport.html pour la liste des points à traiter à la main.")
    return 0 if not res["errors"] else 2


def menu() -> int:
    print(f"swap {__version__} — assistant de migration de poste\n")
    print(" 1) Analyser CE poste et produire le rapport de migration")
    print(" 2) Copier les données vers un disque / un partage réseau")
    print(" 3) Vérifier une sauvegarde")
    print(" 4) Restaurer sur le NOUVEAU poste")
    print(" q) Quitter")
    choice = input("\nVotre choix : ").strip().lower()
    ns = argparse.Namespace(out=DEFAULT_OUT, inventory=None, no_system=False, only=None, skip=None, interactive=True, yes=False,
                            dry_run=False, hash=False, deep=False, overwrite=False)
    if choice == "1":
        return cmd_scan(ns)
    if choice == "2":
        ns.dest = input("Dossier de destination (ex. E:\\Migration ou \\\\serveur\\partage\\migration) : ").strip().strip('"')
        return cmd_copy(ns)
    if choice == "3":
        ns.backup = input("Dossier de la sauvegarde (SWAP-...) : ").strip().strip('"')
        return cmd_verify(ns)
    if choice == "4":
        ns.backup = input("Dossier de la sauvegarde (SWAP-...) : ").strip().strip('"')
        return cmd_restore(ns)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="swap", description="Assistant de migration d'un ancien poste vers un nouveau.")
    p.add_argument("--version", action="version", version=f"swap {__version__}")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("scan", help="analyser ce poste et produire le rapport de ce qu'il faut migrer")
    s.add_argument("--out", default=DEFAULT_OUT, help="dossier de sortie (défaut : %(default)s)")
    s.add_argument("--no-system", action="store_true", help="ne pas lister imprimantes, tâches, certificats...")
    s.set_defaults(func=cmd_scan)

    c = sub.add_parser("copy", help="copier les données vers un disque externe ou un partage réseau")
    c.add_argument("--dest", required=True, help="dossier de destination")
    c.add_argument("--out", default=DEFAULT_OUT, help="dossier contenant l'inventaire du scan")
    c.add_argument("--inventory", help="fichier inventaire.json (défaut : celui du scan)")
    c.add_argument("--only", nargs="+", help="ne copier que les éléments dont le nom contient ces mots")
    c.add_argument("--skip", nargs="+", help="exclure les éléments dont le nom contient ces mots")
    c.add_argument("-i", "--interactive", action="store_true", help="choisir les éléments à la main")
    c.add_argument("--hash", action="store_true", help="calculer une empreinte SHA-256 de chaque fichier (plus lent)")
    c.add_argument("--dry-run", action="store_true", help="simuler sans rien écrire")
    c.add_argument("-y", "--yes", action="store_true", help="ne pas poser de question")
    c.set_defaults(func=cmd_copy)

    v = sub.add_parser("verify", help="vérifier qu'une sauvegarde est complète")
    v.add_argument("--backup", required=True, help="dossier SWAP-<poste> de la sauvegarde")
    v.add_argument("--deep", action="store_true", help="relire les fichiers et comparer les empreintes (nécessite copy --hash)")
    v.set_defaults(func=cmd_verify)

    r = sub.add_parser("restore", help="remettre les données en place sur le nouveau poste")
    r.add_argument("--backup", required=True, help="dossier SWAP-<poste> de la sauvegarde")
    r.add_argument("--only", nargs="+")
    r.add_argument("--skip", nargs="+")
    r.add_argument("-i", "--interactive", action="store_true")
    r.add_argument("--overwrite", action="store_true", help="écraser les fichiers déjà présents (défaut : ne jamais écraser)")
    r.add_argument("--dry-run", action="store_true")
    r.set_defaults(func=cmd_restore)
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if not args.cmd:
            return menu() if sys.stdin.isatty() else (parser.print_help() or 0)
        return args.func(args)
    except KeyboardInterrupt:
        print("\nInterrompu. Relancez la même commande : la copie reprend là où elle s'est arrêtée.")
        return 130
