"""Logique de l'interface graphique (sans widgets, donc testable) : analyse, sélection, envoi, réception, restauration."""

from __future__ import annotations

import json
import os
import platform
import re
import threading
import traceback
from typing import Callable, Optional

from . import net, transfer
from .locations import Locations, list_profiles
from .model import Item
from .publish import INVENTORY, RULES_FILE, publish_extras, write_outputs
from .redirects import load_rules_file, parse_rules, plan_redirects
from .scan import INSTALLER_CATEGORY, run_scan
from .sink import Cancelled, LocalSink, PlaceSink, SinkError
from .util import human_size


class JobContext:
    """Passé à la tâche : message courant, octets traités, annulation. Lu par l'interface pour se rafraîchir."""

    def __init__(self):
        self.cancel_event = threading.Event()
        self.message = ""
        self.done_bytes = 0
        self.total_bytes = 0
        self.log: list = []

    def check(self) -> None:
        if self.cancel_event.is_set():
            raise Cancelled()

    def progress(self, message: str) -> None:
        self.check()
        self.message = message

    def on_chunk(self, n: int) -> None:
        self.check()
        self.done_bytes += n

    def say(self, line: str) -> None:
        self.log.append(line)


class Job(threading.Thread):
    """Exécute `fn(ctx)` en arrière-plan. Résultat dans .result, erreur dans .error, annulation dans .cancelled."""

    def __init__(self, fn: Callable[[JobContext], object]):
        super().__init__(daemon=True)
        self.fn = fn
        self.ctx = JobContext()
        self.result = None
        self.error: Optional[BaseException] = None
        self.trace = ""
        self.cancelled = False
        self.finished = threading.Event()

    def run(self) -> None:
        try:
            self.result = self.fn(self.ctx)
        except Cancelled:
            self.cancelled = True
        except BaseException as exc:  # noqa: BLE001 - l'interface doit afficher toute erreur au lieu de mourir en silence
            self.error = exc
            self.trace = traceback.format_exc()
        finally:
            self.finished.set()

    def cancel(self) -> None:
        self.ctx.cancel_event.set()


CATEGORY_ORDER = ["Dossiers personnels", "PC SOFT", "Installateurs", "Dossiers hors profil", "Autres disques", "Configuration"]


def category_rank(category: str) -> int:
    """Ordre d'affichage : ce qui compte le plus pour l'utilisateur d'abord, la configuration des applis en dernier."""
    for rank, prefix in enumerate(CATEGORY_ORDER):
        if category.startswith(prefix):
            return rank
    return len(CATEGORY_ORDER)


def split_patterns(text: str) -> list:
    return [p for p in re.split(r"[\s,;]+", text or "") if p]


class Session:
    def __init__(self, out_dir: str = "swap-sortie", loc: Optional[Locations] = None):
        self.out_dir = out_dir
        self.loc = loc
        self.inv: Optional[dict] = None
        self.items: list = []
        self.selected: set = set()
        self.machine = platform.node()
        self.rules_text = ""
        self.exclude_text = ""
        self.last_backup = ""
        self.profiles_base = ""  # dossier contenant les profils (défaut : C:\Users)
        self.source_home = ""  # profil à analyser (vide = compte connecté)
        self.source_host = ""  # PC source distant (vide = ce poste)
        self.source_user = ""  # utilisateur du PC source distant
        self.target_home = ""  # profil dans lequel restaurer (vide = compte connecté)
        self.remote_users_base = None  # tests : fonction hôte -> dossier des profils ; défaut \\\\hôte\\C$\\Users
        self.remote_drive_root = None  # tests : fonction (hôte, lecteur) -> racine ; défaut \\\\hôte\\X$
        self.load_rules_text()

    # -- profils utilisateur --------------------------------------------------------------------------------------
    def profiles(self) -> list:
        return list_profiles(self.profiles_base)

    def set_source(self, host: str = "", user: str = "", home: str = "") -> None:
        """PC source : ce poste (host vide ou égal au nom de ce poste) avec le profil `home`, ou un autre PC et son utilisateur."""
        host = (host or "").strip()
        local = platform.node().lower()
        if host and host.lower() not in (local, local.split(".")[0]):
            self.source_host, self.source_user, self.source_home = host, (user or "").strip(), ""
        else:
            self.source_host = self.source_user = ""
            self.source_home = home

    def source_loc(self) -> Locations:
        if self.source_host:
            if not self.source_user:
                raise SinkError("Choisissez l'utilisateur du PC source (bouton « Charger »).")
            if self.source_user not in [p["name"] for p in self.remote_users(self.source_host)]:
                raise SinkError(f"Le profil « {self.source_user} » n'existe pas sur {self.source_host}.")
            return self.remote_locations(self.source_host, self.source_user)
        return Locations.for_profile(self.source_home) if self.source_home else (self.loc or Locations.detect())

    def target_loc(self) -> Locations:
        return Locations.for_profile(self.target_home) if self.target_home else (self.loc or Locations.detect())

    # -- règles ---------------------------------------------------------------------------------------------------
    def load_rules_text(self) -> None:
        path = os.path.join(self.out_dir, RULES_FILE)
        try:
            with open(path, encoding="utf-8-sig") as fh:
                self.rules_text = "".join(ln for ln in fh if ln.strip() and not ln.lstrip().startswith("#"))
        except OSError:
            self.rules_text = ""

    def rules(self) -> list:
        return parse_rules(self.rules_text.splitlines())

    def excludes(self) -> list:
        return split_patterns(self.exclude_text)

    # -- analyse --------------------------------------------------------------------------------------------------
    def set_inventory(self, inv: dict) -> None:
        self.inv = inv
        self.machine = inv["meta"]["machine"]
        self.items = [Item.from_dict(d) for d in inv["items"]]
        self.selected = {i.id for i in self.items if i.default}

    def load_inventory(self, path: str) -> None:
        with open(path, encoding="utf-8") as fh:
            self.set_inventory(json.load(fh))

    def scan(self, ctx: JobContext) -> dict:
        inv = run_scan(self.source_loc(), progress=ctx.progress)
        ctx.progress("Écriture du rapport...")
        write_outputs(inv, self.out_dir)
        self.set_inventory(inv)
        return inv

    def report_path(self) -> str:
        return os.path.abspath(os.path.join(self.out_dir, "rapport.html"))

    def summary_lines(self) -> list:
        if not self.inv:
            return []
        inv = self.inv
        chosen = self.selected_items()
        lines = [
            f"{len(self.items)} élément(s) trouvé(s) — {len(chosen)} proposé(s) par défaut, {human_size(self.total_size())}.",
            f"{len([a for a in inv['apps'] if not a.get('component')])} application(s) installée(s).",
            "",
        ]
        for level, mark in (("critique", "CRITIQUE"), ("important", "Important")):
            for a in inv["advice"]:
                if a["level"] == level:
                    lines.append(f"[{mark}] {a['text']}")
                    lines.append("")
        return lines

    # -- sélection ------------------------------------------------------------------------------------------------
    def selected_items(self) -> list:
        return [i for i in self.items if i.id in self.selected]

    def total_size(self) -> int:
        return sum(i.size for i in self.selected_items())

    def toggle(self, item_id: str) -> None:
        self.selected.symmetric_difference_update({item_id})

    def select_all(self, value: bool) -> None:
        self.selected = {i.id for i in self.items} if value else set()

    def select_defaults(self) -> None:
        self.selected = {i.id for i in self.items if i.default}

    def filtered(self, text: str) -> list:
        text = (text or "").strip().lower()
        if not text:
            return list(self.items)
        return [i for i in self.items if text in f"{i.label} {i.category} {i.src} {i.id}".lower()]

    # -- envoi ----------------------------------------------------------------------------------------------------
    def open_sink(self, mode: str, *, host: str = "", code: str = "", port: int = net.DEFAULT_PORT, folder: str = "",
                  share: str = "C$", subfolder: str = "SWAP", user: str = ""):
        """mode : "direct" (PC en réception), "share" (\\\\PC\\C$\\...) ou "folder" (disque / dossier)."""
        user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
        if mode == "direct":
            return net.NetSink(host.strip(), code, port, self.machine, user)
        if mode == "share":
            base = net.unc_path(host, share, subfolder)
            return LocalSink(transfer.backup_dir_for(base, self.machine))
        if mode == "folder":
            if not folder.strip():
                raise SinkError("Choisissez un dossier de destination.")
            return LocalSink(transfer.backup_dir_for(folder.strip(), self.machine))
        raise ValueError(mode)

    def test_destination(self, mode: str, **kw) -> str:
        """Vérifie la destination sans rien envoyer ; renvoie un message lisible ou lève SinkError."""
        if mode == "direct":
            info = net.probe(kw["host"].strip(), kw["code"], kw.get("port", net.DEFAULT_PORT))
            free = f", {human_size(info['free'])} libres" if info["free"] >= 0 else ""
            return f"Connexion établie avec {info['machine'] or kw['host']}{free}. Le code est correct."
        if mode == "remote":
            names = [p["name"] for p in self.remote_users(kw["host"])]
            user = kw.get("user", "")
            if user and user not in names:
                raise SinkError(f"Le profil « {user} » n'existe pas sur {kw['host'].strip()} (profils trouvés : {', '.join(names)}). "
                                "Il doit s'être connecté au moins une fois sur ce PC.")
            return f"{kw['host'].strip()} est joignable ; profils trouvés : {', '.join(names)}." + (f" Les données iront dans le profil « {user} »." if user else "")
        if mode == "share":
            path = net.unc_path(kw["host"], kw.get("share", "C$"), "")
            if not net.check_folder(path):
                raise SinkError(f"{path} est inaccessible. Le PC est-il allumé, le nom correct, et avez-vous les droits administrateur dessus ?")
            return f"{path} est accessible : la sauvegarde ira dans {net.unc_path(kw['host'], kw.get('share', 'C$'), kw.get('subfolder', 'SWAP'))}."
        folder = kw.get("folder", "").strip()
        if not folder:
            raise SinkError("Choisissez un dossier de destination.")
        os.makedirs(folder, exist_ok=True)
        return f"{folder} est accessible."

    def send(self, ctx: JobContext, sink, *, dry_run: bool = False, want_hash: bool = False) -> dict:
        items = self.selected_items()
        if not items:
            raise SinkError("Rien n'est coché dans l'onglet « Choisir ».")
        ctx.total_bytes = sum(i.size for i in items if i.kind != "registry")
        ctx.done_bytes = 0
        try:
            summary = transfer.run_backup(items, self.inv, sink=sink, dry_run=dry_run, want_hash=want_hash, progress=ctx.progress,
                                          redirects=self.rules(), exclude_files=self.excludes(), on_chunk=ctx.on_chunk)
            if not dry_run:
                ctx.progress("Envoi du rapport et de l'outil de restauration...")
                publish_extras(self.inv, sink)
                self.last_backup = summary["backup"]
        finally:
            sink.close()
        return summary

    # -- installation directe sur un autre PC (tout depuis ce poste) -----------------------------------------------
    def _remote_args(self, host: str) -> tuple:
        base = self.remote_users_base(host) if self.remote_users_base else ""
        root = (lambda d: self.remote_drive_root(host, d)) if self.remote_drive_root else None
        return base, root

    def remote_users(self, host: str) -> list:
        """Profils présents sur le PC cible (par \\\\PC\\C$\\Users). Lève SinkError si le PC n'est pas joignable."""
        host = host.strip()
        if not host:
            raise SinkError("Saisissez le nom (ou l'adresse) du PC cible, en haut de la page.")
        base, _root = self._remote_args(host)
        base = base or net.unc_path(host, "C$", "Users")
        if not net.check_folder(base):
            raise SinkError(f"{base} est inaccessible. Le PC est-il allumé, le nom correct, et êtes-vous administrateur sur ce PC ?")
        profiles = list_profiles(base, connected=False)
        if not profiles:
            raise SinkError(f"Aucun profil utilisateur trouvé dans {base}. L'utilisateur doit s'être connecté au moins une fois sur le PC cible.")
        return profiles

    def remote_locations(self, host: str, user: str) -> Locations:
        base, root = self._remote_args(host)
        return Locations.for_remote(host, user, base, root)

    def send_remote(self, ctx: JobContext, host: str, user: str, *, dry_run: bool = False, want_hash: bool = False,
                    overwrite: bool = False) -> dict:
        """Copie la sélection directement dans le profil `user` du PC `host` (Documents, Bureau, AppData...), sans rien lancer là-bas.

        Les réglages du registre ne peuvent pas être posés à distance : ils sont comptés dans `summary["skipped_registry"]`.
        Un dossier SWAP-<poste> reçoit le rapport, le manifeste et le script d'installation des applications."""
        host = host.strip()
        chosen = self.selected_items()
        if not chosen:
            raise SinkError("Rien n'est coché dans l'onglet « Choisir ».")
        if user not in [p["name"] for p in self.remote_users(host)]:
            raise SinkError(f"Le profil « {user} » n'existe pas sur {host}. Il doit s'être connecté au moins une fois sur ce PC.")
        loc = self.remote_locations(host, user)
        skipped_registry = [i for i in chosen if i.kind == "registry"]
        items = [i for i in chosen if i.kind != "registry"]
        redirected = plan_redirects(items, self.rules())
        place = {}
        for item in items:
            if item.category == INSTALLER_CATEGORY:
                continue   # restent dans le dossier SWAP-<poste> : le script d'installation les y retrouve
            final = loc.remote_path(redirected[item.id]) if item.id in redirected else loc.resolve(item.target)
            place[item.id] = (final, "file" if item.kind == "file" else "dir")
        meta = transfer.backup_dir_for(loc.remote_path("C:\\SWAP"), self.machine)
        sink = PlaceSink(meta, place, overwrite)
        ctx.total_bytes = sum(i.size for i in items)
        ctx.done_bytes = 0
        ctx.say(f"Destination : profil « {user} » sur {host} ({loc.home})")
        if skipped_registry:
            ctx.say(f"{len(skipped_registry)} réglage(s) du registre non posés à distance : " + ", ".join(i.label for i in skipped_registry))
        try:
            summary = transfer.run_backup(items, self.inv, sink=sink, dry_run=dry_run, want_hash=want_hash, progress=ctx.progress,
                                          redirects=self.rules(), exclude_files=self.excludes(), on_chunk=ctx.on_chunk,
                                          extra_manifest={"placed": {"host": host, "user": user}})
            if not dry_run:
                ctx.progress("Dépôt du rapport et du script d'installation...")
                publish_extras(self.inv, sink)
        finally:
            sink.close()
        summary["skipped_registry"] = [i.label for i in skipped_registry]
        summary["placed_in"] = loc.home
        summary["report_dir"] = meta
        return summary

    # -- restauration ---------------------------------------------------------------------------------------------
    def restore(self, ctx: JobContext, backup: str, *, overwrite: bool = False, dry_run: bool = False, rules_text: str = "") -> dict:
        manifest = transfer.load_manifest(backup)
        ctx.total_bytes = sum(s.get("bytes", 0) for s in manifest.get("stats", {}).values())
        ctx.done_bytes = 0
        extra = parse_rules(rules_text.splitlines()) + load_rules_file(os.path.join(backup, RULES_FILE))
        return transfer.run_restore(backup, self.target_loc(), overwrite=overwrite, dry_run=dry_run,
                                    rules=extra, on_chunk=ctx.on_chunk)


def find_backups(folder: str) -> list:
    """Dossiers SWAP-* (avec manifeste) sous `folder`, ou `folder` lui-même."""
    found = []
    if os.path.exists(os.path.join(folder, transfer.MANIFEST)):
        found.append(folder)
    try:
        for name in sorted(os.listdir(folder)):
            path = os.path.join(folder, name)
            if name.startswith("SWAP-") and os.path.exists(os.path.join(path, transfer.MANIFEST)):
                found.append(path)
    except OSError:
        pass
    return found
