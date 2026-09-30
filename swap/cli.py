"""Ligne de commande : analyser, copier, vérifier, restaurer."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

from . import __version__, transfer
from .sink import LocalSink, SinkError
from .locations import Locations
from .model import Item
from .redirects import load_rules_file, parse_rules
from .publish import INVENTORY, RULES_FILE, publish_extras, write_outputs
from .scan import run_scan
from .util import human_size

DEFAULT_OUT = "swap-sortie"


def _progress(msg: str) -> None:
    if sys.stderr.isatty():
        cols = shutil.get_terminal_size((100, 20)).columns - 1
        sys.stderr.write("\r" + msg[:cols].ljust(cols))
        sys.stderr.flush()


def _end_progress() -> None:
    if sys.stderr.isatty():
        sys.stderr.write("\r" + " " * (shutil.get_terminal_size((100, 20)).columns - 1) + "\r")


def _rules(args) -> list:
    """Règles de destination : --map en ligne de commande, puis le fichier de règles."""
    rules = parse_rules(args.map or [])
    path = args.map_file or os.path.join(args.out, RULES_FILE)
    return rules + load_rules_file(path)


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
    print(f"Règles de destination (ex. « les projets PC SOFT arrivent dans C:\\Mes Projets ») : {paths['regles']}")
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


def _make_sink(args, inv):
    """Destination : PC distant en connexion directe (--host/--code) ou dossier (--dest)."""
    if args.host:
        from .net import NetSink

        if not args.code:
            raise SystemExit("--code est obligatoire avec --host (le code affiché sur le PC cible).")
        return NetSink(args.host, args.code, args.port, inv["meta"]["machine"], inv["meta"]["user"])
    if not args.dest:
        raise SystemExit("Indiquez --dest (dossier) ou --host et --code (PC cible).")
    return LocalSink(transfer.backup_dir_for(args.dest, inv["meta"]["machine"]))


def cmd_copy(args) -> int:
    inv = _load_or_scan(args)
    items = [Item.from_dict(d) for d in inv["items"]]
    chosen = transfer.select_items(items, args.only or (), args.skip or (), args.interactive)
    if not chosen:
        print("Rien à copier.")
        return 1
    try:
        sink = _make_sink(args, inv)
    except SinkError as exc:
        print(f"\n{exc}")
        return 2
    try:
        return _copy_to(args, inv, chosen, sink)
    finally:
        sink.close()


def _copy_to(args, inv, chosen, sink) -> int:
    need, free = transfer.check_space(chosen, args.dest) if not args.host else (sum(i.size for i in chosen), -1)
    print(f"\n{len(chosen)} élément(s) à copier, {human_size(need)} → {sink.describe()}")
    if 0 <= free < need:
        print(f"ATTENTION : seulement {human_size(free)} libres sur la destination.")
        if not args.yes and not args.dry_run and input("Continuer quand même ? [o/N] ").strip().lower() not in ("o", "oui", "y"):
            return 1
    if any(i.sensitive for i in chosen):
        print("ATTENTION : la copie contient des données sensibles (🔒) : utilisez un support chiffré.")
    if not args.dry_run and not args.yes and input("Lancer la copie ? [O/n] ").strip().lower() in ("n", "non"):
        return 1
    rules = _rules(args)
    if rules:
        print("Règles de destination enregistrées avec la sauvegarde :")
        for pattern, dest in rules:
            print(f"  {pattern}  →  {dest}")
    try:
        summary = transfer.run_backup(chosen, inv, sink=sink, dry_run=args.dry_run, want_hash=args.hash, progress=_progress,
                                      redirects=rules, exclude_files=args.exclude or ())
    except SinkError as exc:
        _end_progress()
        print(f"\nTransfert interrompu : {exc}\nRelancez la même commande : seuls les fichiers manquants seront renvoyés.")
        return 2
    _end_progress()
    for item in chosen:
        r = summary["items"][item.id]
        print(f"  {item.label:55} {r.get('copied', 0):>7} copiés, {r.get('unchanged', 0):>7} déjà à jour"
              + (f", {len(r['errors'])} erreur(s)" if r["errors"] else ""))
    if args.dry_run:
        print("\n(simulation : rien n'a été écrit)")
        return 0
    publish_extras(inv, sink)
    print(f"\nSauvegarde : {summary['backup']}")
    if summary["errors"]:
        print(f"{len(summary['errors'])} fichier(s) non copié(s) (verrouillés ou inaccessibles) : voir erreurs.log")
        print("Astuce : fermez Outlook/les applications concernées et relancez la même commande, seuls les fichiers manquants seront recopiés.")
    print("Sur le nouveau poste : lancez RESTAURER.bat depuis ce dossier.")
    return 0 if not summary["errors"] else 2


def cmd_receive(args) -> int:
    """PC cible : attend l'envoi d'un autre PC (connexion directe chiffrée, protégée par un code)."""
    from . import net

    state = {"last": 0.0}

    def on_event(e):
        kind = e["kind"]
        if kind == "connected":
            print(f"\nConnexion de {e['machine']} ({e['peer']}), utilisateur {e['user']} → {e['root']}")
        elif kind == "file":
            import time

            if time.monotonic() - state["last"] > 0.5:
                state["last"] = time.monotonic()
                _progress(f"{e['files']} fichiers, {human_size(e['bytes'])} reçus")
        elif kind == "done":
            _end_progress()
            print(f"Terminé : {e['files']} fichier(s), {human_size(e['bytes'])} dans {e['root']}")
        elif kind in ("refused", "aborted"):
            _end_progress()
            print(f"{'Connexion refusée' if kind == 'refused' else 'Transfert interrompu'} ({e['peer']}) : {e['error']}")

    rx = net.Receiver(args.dest, args.code, args.port, on_event)
    print(f"Ce PC : {socket_name()}  —  adresses : {', '.join(net.local_addresses()) or '?'}")
    print(f"Code à saisir sur le PC source : {rx.code}   (port {rx.port})")
    if args.firewall:
        ok, msg = net.firewall_open(rx.port)
        print("Pare-feu : port ouvert." if ok else f"Pare-feu : impossible de l'ouvrir ({msg}). Lancez en administrateur ou ouvrez le port TCP {rx.port}.")
    print(f"Réception dans : {args.dest}   (Ctrl+C pour arrêter)")
    try:
        rx.serve(once=not args.keep)
    except KeyboardInterrupt:
        rx.stop()
        print("\nRéception arrêtée.")
    finally:
        if args.firewall:
            net.firewall_close(rx.port)
    root = rx.root
    if root:
        print(f"\nSur ce PC, restaurez avec : python -m swap restore --backup \"{root}\"   (ou RESTAURER.bat dans ce dossier)")
    return 0


def socket_name() -> str:
    import socket

    return socket.gethostname()


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
    rules = parse_rules(args.map or []) + load_rules_file(args.map_file or os.path.join(args.backup, RULES_FILE))
    res = transfer.run_restore(args.backup, loc, only=args.only or (), skip=args.skip or (), overwrite=args.overwrite,
                               dry_run=args.dry_run, interactive=args.interactive, rules=rules)
    for item_id, r in res["items"].items():
        print(f"  {item_id:45} {r['restored']:>7} restauré(s), {r['skipped']:>7} déjà présent(s)"
              + (f", {len(r['errors'])} erreur(s)" if r["errors"] else ""))
        if r.get("dest"):
            print(f"      → {r['dest']}")
    for e in res["errors"][:20]:
        print("  !", e)
    script = os.path.join(args.backup, "installer_et_configurer.ps1")
    if os.path.exists(script):
        print(f"\nÉtape suivante : relisez puis lancez {script} dans PowerShell (lecteurs réseau, imprimantes, applications).")
    print("Puis consultez rapport.html pour la liste des points à traiter à la main.")
    return 0 if not res["errors"] else 2


def cmd_gui(args) -> int:
    try:
        from .gui import run
    except ImportError as exc:  # tkinter absent (Python installé sans Tcl/Tk)
        print(f"Interface graphique indisponible ({exc}). Utilisez la ligne de commande : python -m swap --help")
        return 1
    return run(args.out, getattr(args, "backup", None))


def menu() -> int:
    print(f"swap {__version__} — assistant de migration de poste\n")
    print(" 1) Analyser CE poste et produire le rapport de migration")
    print(" 2) Copier les données vers un disque / un partage réseau")
    print(" 3) Vérifier une sauvegarde")
    print(" 4) Restaurer sur le NOUVEAU poste")
    print(" q) Quitter")
    choice = input("\nVotre choix : ").strip().lower()
    ns = argparse.Namespace(host=None, code=None, port=47800, dest=None, exclude=None, map=None, map_file=None, out=DEFAULT_OUT, inventory=None, no_system=False, only=None, skip=None, interactive=True, yes=False,
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

    g = sub.add_parser("gui", help="ouvrir l'interface graphique")
    g.add_argument("--out", default=os.path.abspath(DEFAULT_OUT), help="dossier de sortie (défaut : %(default)s)")
    g.add_argument("--backup", help="ouvrir directement la restauration de cette sauvegarde (utilisé par RESTAURER.bat)")
    g.set_defaults(func=cmd_gui)

    s = sub.add_parser("scan", help="analyser ce poste et produire le rapport de ce qu'il faut migrer")
    s.add_argument("--out", default=DEFAULT_OUT, help="dossier de sortie (défaut : %(default)s)")
    s.add_argument("--no-system", action="store_true", help="ne pas lister imprimantes, tâches, certificats...")
    s.set_defaults(func=cmd_scan)

    c = sub.add_parser("copy", help="copier les données vers un disque externe ou un partage réseau")
    c.add_argument("--dest", help="dossier de destination (disque externe, partage \\\\serveur\\partage...)")
    c.add_argument("--host", help="nom ou adresse du PC cible (connexion directe ; il doit être en mode réception)")
    c.add_argument("--code", help="code affiché par le PC cible en mode réception")
    c.add_argument("--port", type=int, default=47800, help="port de la connexion directe (défaut : %(default)s)")
    c.add_argument("--out", default=DEFAULT_OUT, help="dossier contenant l'inventaire du scan")
    c.add_argument("--inventory", help="fichier inventaire.json (défaut : celui du scan)")
    c.add_argument("--only", nargs="+", help="ne copier que les éléments dont le nom contient ces mots")
    c.add_argument("--skip", nargs="+", help="exclure les éléments dont le nom contient ces mots")
    c.add_argument("-i", "--interactive", action="store_true", help="choisir les éléments à la main")
    c.add_argument("--map", action="append", metavar="MOTIF=DESTINATION",
                   help="règle de destination, ex. --map \"pcsoft-projet=C:\\Mes Projets\" (répétable ; voir regles.txt)")
    c.add_argument("--map-file", help="fichier de règles (défaut : regles.txt du dossier de sortie)")
    c.add_argument("--exclude", nargs="+", metavar="MOTIF", help="ne pas copier ces fichiers, ex. --exclude *.iso *.msu")
    c.add_argument("--hash", action="store_true", help="calculer une empreinte SHA-256 de chaque fichier (plus lent)")
    c.add_argument("--dry-run", action="store_true", help="simuler sans rien écrire")
    c.add_argument("-y", "--yes", action="store_true", help="ne pas poser de question")
    c.set_defaults(func=cmd_copy)

    rc = sub.add_parser("receive", help="PC cible : recevoir la copie d'un autre PC par le réseau (affiche un code)")
    rc.add_argument("--dest", default="C:\\SWAP", help="dossier où ranger ce qui est reçu (défaut : %(default)s)")
    rc.add_argument("--port", type=int, default=47800)
    rc.add_argument("--code", help="code imposé (sinon un code aléatoire est généré)")
    rc.add_argument("--firewall", action="store_true", help="ouvrir le port dans le pare-feu Windows pendant la réception (administrateur)")
    rc.add_argument("--keep", action="store_true", help="rester en écoute après un premier envoi réussi")
    rc.set_defaults(func=cmd_receive)

    v = sub.add_parser("verify", help="vérifier qu'une sauvegarde est complète")
    v.add_argument("--backup", required=True, help="dossier SWAP-<poste> de la sauvegarde")
    v.add_argument("--deep", action="store_true", help="relire les fichiers et comparer les empreintes (nécessite copy --hash)")
    v.set_defaults(func=cmd_verify)

    r = sub.add_parser("restore", help="remettre les données en place sur le nouveau poste")
    r.add_argument("--backup", required=True, help="dossier SWAP-<poste> de la sauvegarde")
    r.add_argument("--only", nargs="+")
    r.add_argument("--skip", nargs="+")
    r.add_argument("-i", "--interactive", action="store_true")
    r.add_argument("--map", action="append", metavar="MOTIF=DESTINATION", help="règle de destination (priment sur celles de la sauvegarde)")
    r.add_argument("--map-file", help="fichier de règles (défaut : regles.txt de la sauvegarde)")
    r.add_argument("--overwrite", action="store_true", help="écraser les fichiers déjà présents (défaut : ne jamais écraser)")
    r.add_argument("--dry-run", action="store_true")
    r.set_defaults(func=cmd_restore)
    return p


def _gui_possible() -> bool:
    """Interface graphique par défaut sous Windows (ou si un écran est disponible) quand tkinter est installé."""
    if os.name != "nt" and not os.environ.get("DISPLAY"):
        return False
    try:
        import tkinter  # noqa: F401
    except ImportError:
        return False
    return True


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if not args.cmd:
            if _gui_possible():
                return cmd_gui(argparse.Namespace(out=os.path.abspath(DEFAULT_OUT)))
            return menu() if sys.stdin.isatty() else (parser.print_help() or 0)
        return args.func(args)
    except KeyboardInterrupt:
        print("\nInterrompu. Relancez la même commande : la copie reprend là où elle s'est arrêtée.")
        return 130
