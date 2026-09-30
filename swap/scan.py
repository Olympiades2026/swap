"""Analyse du poste : construit l'inventaire complet (données, applis, configuration)."""

from __future__ import annotations

import ctypes
import os
import platform
import re
import time
from datetime import datetime
from typing import Callable, Optional

from . import __version__, apps, system
from .advice import compute_advice
from .catalog import APP_CONFIGS, EXT_HINTS
from .fs import TypeSink, collect, is_hidden_or_system, make_excluder
from .locations import KNOWN_FOLDERS, Locations
from .model import Item
from .util import is_under, is_windows, slugify

# Dossiers à la racine d'un disque qui appartiennent à Windows ou aux logiciels : jamais proposés.
SYSTEM_ROOT_DIRS = {
    "windows", "program files", "program files (x86)", "programdata", "users", "recovery", "perflogs", "intel", "msocache",
    "$windows.~bt", "$windows.~ws", "$winreagent", "config.msi", "documents and settings", "system volume information",
    "$recycle.bin", "windows.old", "amd", "nvidia", "drivers", "inetpub",
}
SKIP_HOME_DIRS = {"appdata", "application data", "local settings", "3d objects", "searches", "links", "saved games", "contacts", "favorites"}


def fixed_drives() -> list:
    """Lettres des disques fixes (C:\\, D:\\...). Sur un autre système : la racine seule."""
    if not is_windows():
        return []
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    drives = []
    for i in range(26):
        if mask & (1 << i):
            root = f"{chr(65 + i)}:\\"
            if ctypes.windll.kernel32.GetDriveTypeW(root) == 3:  # DRIVE_FIXED
                drives.append(root)
    return drives


class _Scanner:
    def __init__(self, loc: Locations, progress: Optional[Callable[[str], None]]):
        self.loc = loc
        self.progress = progress or (lambda _msg: None)
        self.sink = TypeSink()
        self.items: list = []
        self.onedrive = loc.onedrive_roots()
        self._last = 0.0

    def _tick(self, label):
        def cb(root, stats):
            now = time.monotonic()
            if now - self._last > 0.25:
                self._last = now
                self.progress(f"{label} : {stats.files} fichiers")
        return cb

    def add(self, item_id, label, kind, src, target, category, *, cache=False, extra=(), track=False, **kw):
        self.progress(f"Analyse : {label}")
        exc = make_excluder(cache=cache, extra=extra)
        stats = collect(src, exc, self.sink if track else None, self._tick(label))
        item = Item(
            id=item_id, label=label, kind=kind, src=src, target=target, category=category, size=stats.size, files=stats.files,
            cloud_files=stats.placeholders, cache_excludes=cache, extra_excludes=list(extra), **kw,
        )
        self.items.append(item)
        return item

    # -- données de l'utilisateur ------------------------------------------------------------------------
    def known_folders(self):
        for name, (label, _reg, _default) in KNOWN_FOLDERS.items():
            path = self.loc.known.get(name)
            if not path or not os.path.isdir(path):
                continue
            in_od = self.loc.in_onedrive(path)
            note = ""
            if in_od:
                note = ("Synchronisé avec OneDrive : il se retrouvera en se connectant sur le nouveau poste "
                        "(les fichiers « en ligne uniquement » ne sont de toute façon pas copiés). Cochez pour copier quand même.")
            self.add(f"dossier-{name.lower()}", label, "dir", path, {"kind": "known", "name": name}, "Dossiers personnels",
                     track=True, default=not in_od, note=note)

    def home_extras(self):
        known_paths = list(self.loc.known.values())
        try:
            names = sorted(os.listdir(self.loc.home), key=str.lower)
        except OSError:
            return
        for name in names:
            path = os.path.join(self.loc.home, name)
            if name.startswith(".") or name.lower() in SKIP_HOME_DIRS or not os.path.isdir(path) or os.path.islink(path):
                continue
            try:
                if is_hidden_or_system(os.stat(path)):
                    continue
            except OSError:
                continue
            if any(is_under(path, r) for r in self.onedrive) or any(is_under(k, path) or is_under(path, k) for k in known_paths):
                continue
            self.add(f"profil-{slugify(name)}", f"Dossier du profil : {name}", "dir", path, {"kind": "home", "rel": name},
                     "Dossiers personnels", track=True)

    def app_configs(self):
        roots = {"appdata": self.loc.appdata, "localappdata": self.loc.localappdata, "home": self.loc.home}
        for cfg in APP_CONFIGS:
            for i, (root, rel) in enumerate(cfg.paths):
                path = os.path.join(roots[root], *rel.split("/"))
                if not os.path.exists(path):
                    continue
                suffix = "" if len(cfg.paths) == 1 else f"-{i + 1}"
                self.add(f"config-{cfg.id}{suffix}", cfg.label, "dir" if os.path.isdir(path) else "file", path,
                         {"kind": root, "rel": rel}, "Configuration des applications", cache=cfg.cache,
                         extra=cfg.extra_excludes, default=cfg.default, sensitive=cfg.sensitive, note=cfg.note)
            if is_windows():
                for j, key in enumerate(cfg.registry):
                    if _registry_key_exists(key):
                        self.items.append(Item(
                            id=f"registre-{cfg.id}{'' if len(cfg.registry) == 1 else f'-{j + 1}'}", label=cfg.label + " (registre)",
                            kind="registry", src=key, target={"kind": "registry"}, category="Configuration des applications",
                            default=cfg.default, sensitive=cfg.sensitive, note=cfg.note))

    def outside_profile(self):
        """Dossiers posés à la racine des disques (C:\\Projets, D:\\Data...) : là où on oublie le plus de choses."""
        for root in fixed_drives():
            try:
                names = sorted(os.listdir(root), key=str.lower)
            except OSError:
                continue
            system_drive = os.path.normcase(root).startswith(os.path.normcase(os.environ.get("SystemDrive", "C:")))
            for name in names:
                path = os.path.join(root, name)
                if name.lower() in SYSTEM_ROOT_DIRS or name.startswith("$") or not os.path.isdir(path) or os.path.islink(path):
                    continue
                try:
                    if is_hidden_or_system(os.stat(path)):
                        continue
                except OSError:
                    continue
                category = "Dossiers hors profil (disque système)" if system_drive else "Autres disques"
                self.add(f"disque-{slugify(root[:1])}-{slugify(name)}", f"{root}{name}", "dir", path, {"kind": "abs", "path": path},
                         category, track=True,
                         note="Peut contenir un logiciel installé (à réinstaller plutôt que copier) : vérifiez avant de cocher.")


def _registry_key_exists(key: str) -> bool:
    try:
        import winreg

        hive, _, sub = key.partition("\\")
        winreg.CloseKey(winreg.OpenKey(getattr(winreg, {"HKCU": "HKEY_CURRENT_USER"}.get(hive, hive)), sub))
        return True
    except (ImportError, OSError, AttributeError):
        return False


def dedupe_overlaps(items: list) -> None:
    """Si un élément est contenu dans un autre, on ne copie que le plus grand (évite les doublons)."""
    dirs = [it for it in items if it.kind == "dir"]
    for a in items:
        if a.kind == "registry":
            continue
        for b in dirs:
            if a is not b and is_under(a.src, b.src) and a.default and b.default and os.path.normcase(a.src) != os.path.normcase(b.src):
                a.default = False
                a.note = (a.note + " " if a.note else "") + f"Déjà inclus dans « {b.label} »."
                break


def run_scan(loc: Optional[Locations] = None, progress: Optional[Callable[[str], None]] = None, with_system: bool = True) -> dict:
    loc = loc or Locations.detect()
    scanner = _Scanner(loc, progress)
    scanner.known_folders()
    scanner.home_extras()
    scanner.app_configs()
    scanner.outside_profile()
    dedupe_overlaps(scanner.items)

    if progress:
        progress("Applications installées...")
    app_list = apps.installed_apps()
    if app_list and progress:
        progress("Correspondance avec winget...")
    winget = apps.winget_map() if app_list else {}
    for app in app_list:
        app["winget_id"] = winget.get(app["name"], "")

    sysinfo = system.collect_all(loc.appdata) if with_system and is_windows() else {}

    sink = scanner.sink
    hints = []
    names = " | ".join(a["name"].lower() for a in app_list)
    for ext, (label, app_re, advice) in EXT_HINTS.items():
        count = sink.exts.get(ext, 0)
        if count:
            installed = bool(app_re and re.search(app_re, names, re.I)) if app_list else None
            hints.append({"ext": ext, "software": label, "count": count, "installed": installed, "advice": advice})
    hints.sort(key=lambda h: -h["count"])

    inv = {
        "meta": {
            "tool_version": __version__,
            "date": datetime.now().isoformat(timespec="seconds"),
            "machine": platform.node(),
            "user": os.environ.get("USERNAME") or os.environ.get("USER") or "",
            "domain": os.environ.get("USERDOMAIN", ""),
            "os": platform.platform(),
            "windows": is_windows(),
            "home": loc.home,
        },
        "items": [it.to_dict() for it in scanner.items],
        "apps": app_list,
        "winget_available": bool(winget),
        "file_types": sink.exts.most_common(40),
        "type_hints": hints,
        "notable_files": {k: v for k, v in sink.notable.items()},
        "big_files": [{"size": s, "path": p} for s, p in sorted(sink.big, reverse=True)],
        "onedrive_roots": scanner.onedrive,
        "system": sysinfo,
    }
    inv["advice"] = compute_advice(inv)
    return inv
