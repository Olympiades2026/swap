"""Analyse du poste : construit l'inventaire complet (données, applis, configuration)."""

from __future__ import annotations

import ctypes
import os
import platform
import re
import time
from datetime import datetime
from typing import Callable, Optional

from . import __version__, apps, installers, system
from .advice import compute_advice
from .catalog import APP_CONFIGS, EXT_HINTS, GENERIC_WORDS, INSTALL_KEEP, NOISE_ROOT_RE
from .fs import FILE_ATTRIBUTE_REPARSE_POINT, TypeSink, attrs, collect, is_hidden_or_system, make_excluder
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


# Installation de WinDev/WebDev : C:\PC SOFT\WINDEV Suite SaaS 2025\... (exemples, langages, framework : rien à migrer)
PCSOFT_INSTALL_RE = re.compile(r"[\\/]pc soft[\\/](windev|webdev)[^\\/]*", re.I)


def pcsoft_install_root(path: str):
    m = PCSOFT_INSTALL_RE.search(path)
    return path[: m.end()] if m else None


def is_reparse(path: str) -> bool:
    """Lien symbolique ou jonction (ex. « Menu Démarrer », « Voisinage réseau » dans un profil) : à ne pas suivre."""
    try:
        st = os.lstat(path)
    except OSError:
        return True
    return os.path.islink(path) or bool(attrs(st) & FILE_ATTRIBUTE_REPARSE_POINT)


def drive_tag(root: str) -> str:
    m = re.match(r"^\\\\[^\\]+\\([A-Za-z])\$", root)   # \\PC\C$ (disque d'un autre poste) -> « c »
    if m:
        return m.group(1).lower()
    drive = os.path.splitdrive(root)[0].rstrip(":")
    return slugify(drive) if drive else "x"


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def app_tokens(app_list: list) -> dict:
    """{mot-clé: nom d'appli} pour reconnaître qu'un dossier de C:\ appartient à un logiciel installé."""
    tokens: dict = {}
    for app in app_list:
        for source in (re.sub(r"[\d.]+", " ", app["name"]), app.get("publisher", "")):
            words = re.findall(r"[a-z0-9]+", source.lower())
            cands = [w for w in words if len(w) >= 5 and w not in GENERIC_WORDS]
            if len(words) >= 2:
                cands.append(words[0] + words[1])
            for tok in cands:
                if len(tok) >= 5:
                    tokens.setdefault(tok, app["name"])
    return tokens


def match_app(folder: str, tokens: dict):
    n = _norm(folder)
    for tok, name in tokens.items():
        if n == tok:  # égalité stricte : « Applications_CLMB » ne doit pas être pris pour « Application Compatibility… »
            return name
    return None


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
    def __init__(self, loc: Locations, progress: Optional[Callable[[str], None]], tokens: Optional[dict] = None):
        self.loc = loc
        self.progress = progress or (lambda _msg: None)
        self.tokens = tokens or {}
        self.sink = TypeSink(ignore=lambda p: pcsoft_install_root(p) is not None)
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

    def add(self, item_id, label, kind, src, target, category, *, cache=False, extra=(), track=False, skip_empty=False, **kw):
        self.progress(f"Analyse : {label}")
        base_id, n = item_id, 2
        while any(i.id == item_id for i in self.items):
            item_id, n = f"{base_id}-{n}", n + 1
        exc = make_excluder(cache=cache, extra=extra)
        stats = collect(src, exc, self.sink if track else None, self._tick(label))
        if skip_empty and stats.files == 0 and stats.size == 0:
            return None
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
            if name.startswith(".") or name.lower() in SKIP_HOME_DIRS or not os.path.isdir(path) or is_reparse(path):
                continue
            try:
                if is_hidden_or_system(os.lstat(path)):
                    continue
            except OSError:
                continue
            if any(is_under(path, r) for r in self.onedrive) or any(is_under(k, path) or is_under(path, k) for k in known_paths):
                continue
            self.add(f"profil-{slugify(name)}", f"Dossier du profil : {name}", "dir", path, {"kind": "home", "rel": name},
                     "Dossiers personnels", track=True, skip_empty=True)

    def app_configs(self):
        roots = {"appdata": self.loc.appdata, "localappdata": self.loc.localappdata, "home": self.loc.home,
                 "programdata": self.loc.programdata}
        for cfg in APP_CONFIGS:
            for i, (root, rel) in enumerate(cfg.paths):
                path = os.path.join(roots[root], *rel.split("/"))
                if not os.path.exists(path):
                    continue
                suffix = "" if len(cfg.paths) == 1 else f"-{i + 1}"
                target = self.loc.abs_target(path) if root == "programdata" else {"kind": root, "rel": rel}
                self.add(f"config-{cfg.id}{suffix}", cfg.label, "dir" if os.path.isdir(path) else "file", path,
                         target, "Configuration des applications", cache=cfg.cache,
                         extra=cfg.extra_excludes, default=cfg.default, sensitive=cfg.sensitive, note=cfg.note)
            if is_windows() and self.loc.current:  # le registre lu est celui du compte connecté
                for j, key in enumerate(cfg.registry):
                    if _registry_key_exists(key):
                        self.items.append(Item(
                            id=f"registre-{cfg.id}{'' if len(cfg.registry) == 1 else f'-{j + 1}'}", label=cfg.label + " (registre)",
                            kind="registry", src=key, target={"kind": "registry"}, category="Configuration des applications",
                            default=cfg.default, sensitive=cfg.sensitive, note=cfg.note))

    def classify_root(self, name: str, path: str):
        """(coché par défaut ?, remarque) pour un dossier posé à la racine d'un disque."""
        try:
            children = os.listdir(path)
        except OSError:
            children = []
        if name.lower() == "pc soft" or any(re.match(r"(windev|webdev)", c, re.I) for c in children if os.path.isdir(os.path.join(path, c))):
            return False, ("Installation de WinDev/WebDev (exemples, framework, aide…) : à réinstaller avec l'installateur PC SOFT / "
                           "votre abonnement, pas à copier. Vos préférences (dossier « Personal ») sont proposées à part. Cochez pour tout copier.")
        if NOISE_ROOT_RE.search(name):
            return False, "Dossier technique ou temporaire (installateurs, ISO…) : cochez-le si vous y avez rangé des fichiers à conserver."
        app = match_app(name, self.tokens)
        if app:
            return False, (f"Correspond au logiciel installé « {app} » : à réinstaller plutôt que copier. "
                           "Cochez-le si vous y avez rangé vos propres fichiers.")
        return True, "Vérifiez son contenu : peut contenir un logiciel installé (à réinstaller plutôt que copier)."

    def keep_user_data(self, parent: Item, category: str) -> None:
        """Dans un dossier de logiciel décoché, propose à part ce qui est à vous (sites Laragon, préférences WinDev…)."""
        name = os.path.basename(parent.src.rstrip("\\/"))
        keeps = [(os.path.join(parent.src, sub), f"{name} : {sub}", category) for sub in INSTALL_KEEP.get(name.lower(), [])]
        if name.lower() == "pc soft":
            for child in sorted(os.listdir(parent.src), key=str.lower):
                if re.match(r"(windev|webdev)", child, re.I):
                    keeps.append((os.path.join(parent.src, child, "Personal"), f"PC SOFT : préférences {child} (Personal)",
                                  "PC SOFT (WinDev / WebDev / HFSQL)"))
        for path, label, cat in keeps:
            if not os.path.isdir(path):
                continue
            item = self.add(f"garder-{slugify(name)}-{slugify(os.path.basename(path))}", label, "dir", path,
                            self.loc.abs_target(path), cat, skip_empty=True,
                            note=f"Ce qui vous appartient dans « {name} » (le reste du dossier se réinstalle).")
            if item:
                parent.exclude_paths.append(path)
                parent.size = max(0, parent.size - item.size)
                parent.files = max(0, parent.files - item.files)

    def outside_profile(self):
        """Dossiers posés à la racine des disques (C:\\Projets, D:\\Data...) : là où on oublie le plus de choses."""
        drives = self.loc.drives()
        for root in (fixed_drives() if drives is None else drives):
            try:
                names = sorted(os.listdir(root), key=str.lower)
            except OSError:
                continue
            tag = self.loc.drive_letter(root).lower() if self.loc.remote else drive_tag(root)
            system_drive = (tag == "c") if self.loc.remote else \
                os.path.normcase(root).startswith(os.path.normcase(os.environ.get("SystemDrive", "C:")))
            for name in names:
                path = os.path.join(root, name)
                if name.lower() in SYSTEM_ROOT_DIRS or name.startswith("$") or not os.path.isdir(path) or is_reparse(path):
                    continue
                try:
                    if is_hidden_or_system(os.lstat(path)):
                        continue
                except OSError:
                    continue
                category = "Dossiers hors profil (disque système)" if system_drive else "Autres disques"
                default, note = self.classify_root(name, path)
                item = self.add(f"disque-{tag}-{slugify(name)}", self.loc.local_path(path), "dir", path,
                                self.loc.abs_target(path), category, track=True, skip_empty=True, default=default, note=note)
                if item is not None and not default:
                    self.keep_user_data(item, category)


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


# extensions repérant une racine de projet/données PC SOFT -> (préfixe d'id, libellé)
PCSOFT_ROOTS = {
    ".wdp": ("pcsoft-projet", "PC SOFT : projet"),
    ".wwp": ("pcsoft-projet", "PC SOFT : projet"),
    ".wpp": ("pcsoft-projet", "PC SOFT : projet"),
    ".fic": ("pcsoft-donnees", "PC SOFT : données HFSQL"),
}


def carve_out_roots(scanner: "_Scanner") -> None:
    """Fait de chaque dossier de projet/données PC SOFT un élément à part (donc redirigeable par une règle)."""
    found: dict = {}
    for ext, (prefix, label) in PCSOFT_ROOTS.items():
        for path in scanner.sink.notable.get(ext, []):
            found.setdefault(os.path.dirname(path), (prefix, label))
    roots = sorted(found, key=lambda p: len(p))
    top = [r for r in roots if not any(o != r and is_under(r, o) and found[o][0] == found[r][0] for o in roots)]
    for root in top:
        prefix, label = found[root]
        name = os.path.basename(root.rstrip("\\/")) or root
        parents = [i for i in scanner.items if i.kind == "dir" and is_under(root, i.src) and os.path.normcase(i.src) != os.path.normcase(root)]
        parent = max(parents, key=lambda i: len(i.src), default=None)
        in_od = scanner.loc.in_onedrive(root)
        item = scanner.add(f"{prefix}-{slugify(name)}", f"{label} {name}", "dir", root, scanner.loc.target_for(root),
                           "PC SOFT (WinDev / WebDev / HFSQL)", default=not in_od,
                           note="Dossier repéré grâce à ses fichiers PC SOFT ; redirigeable avec une règle de destination."
                           + (" Fichiers HFSQL : à copier applications/service arrêtés." if prefix == "pcsoft-donnees" else ""))
        if parent is not None:
            parent.exclude_paths.append(root)
            parent.size = max(0, parent.size - item.size)
            parent.files = max(0, parent.files - item.files)
            parent.note = (parent.note + " " if parent.note else "") + f"« {item.label} » en est extrait et traité à part."
    # un dossier extrait ne doit plus être « déjà inclus » dans un parent : les éléments carved sont indépendants


INSTALLER_CATEGORY = "Installateurs des applications à installer à la main"


def collect_installers(scanner: "_Scanner", app_list: list, sysinfo: dict) -> dict:
    """Rattache aux applis non installables par winget les fichiers d'installation trouvés sur le poste, et en fait des éléments à copier."""
    manual = [a for a in app_list if not a.get("component") and not a.get("winget_id")]
    matches = installers.match_installers(manual, scanner.sink.installers)
    info: dict = {"matched": [], "missing": [], "others": [], "shares": installers.install_shares(sysinfo.get("drives", []))}
    used: set = set()
    for app in manual:
        best = next((c for c in matches.get(app["name"], []) if c["path"] not in used), None)
        if best is None:
            info["missing"].append(app["name"])
            continue
        used.add(best["path"])
        base = os.path.basename(best["path"])
        item = scanner.add(f"installateur-{slugify(app['name'])}", f"Installateur : {app['name']}", "file", best["path"],
                           {"kind": "known", "name": "Downloads", "rel": f"Installateurs/{base}"}, INSTALLER_CATEGORY,
                           note="Retrouvé grâce au nom du fichier : vérifiez qu'il s'agit bien de la version voulue.")
        parents = [i for i in scanner.items if i.kind == "dir" and is_under(best["path"], i.src)]
        parent = max(parents, key=lambda i: len(i.src), default=None)
        if parent is not None:  # évite de copier le même fichier deux fois
            parent.exclude_paths.append(best["path"])
            parent.size = max(0, parent.size - item.size)
            parent.files = max(0, parent.files - 1)
        info["matched"].append({
            "app": app["name"], "version": app.get("version", ""), "item_id": item.id, "file": base, "path": best["path"],
            "size": best["size"], "alternatives": [c["path"] for c in matches[app["name"]] if c["path"] != best["path"]],
        })
    info["others"] = installers.unmatched_installers(scanner.sink.installers, used)
    return info


def run_scan(loc: Optional[Locations] = None, progress: Optional[Callable[[str], None]] = None, with_system: bool = True) -> dict:
    loc = loc or Locations.detect()
    if progress:
        progress("Applications installées...")
    app_list = apps.remote_installed_apps(loc.host) if loc.remote else apps.installed_apps(current_user=loc.current)
    scanner = _Scanner(loc, progress, app_tokens(app_list))
    scanner.known_folders()
    scanner.home_extras()
    scanner.app_configs()
    scanner.outside_profile()
    dedupe_overlaps(scanner.items)
    carve_out_roots(scanner)

    if app_list and progress:
        progress("Correspondance avec winget...")
    winget = apps.winget_map() if (app_list and not loc.remote) else {}   # winget interroge CE poste : inutile pour un autre
    for app in app_list:
        app["winget_id"] = winget.get(app["name"], "")
        app["winget_guess"] = "" if app["winget_id"] or app.get("component") else apps.guess_winget(app["name"])

    sysinfo = system.collect_all(loc.appdata, loc.current) if (with_system and is_windows() and not loc.remote) else {}
    installer_info = collect_installers(scanner, app_list, sysinfo)

    sink = scanner.sink
    hints = []
    names = " | ".join(a["name"].lower() for a in app_list)
    for ext, (label, app_re, advice) in EXT_HINTS.items():
        count = sink.exts.get(ext, 0)
        if count:
            installed = bool(re.search(app_re, names, re.I)) if (app_list and app_re) else None
            hints.append({"ext": ext, "software": label, "count": count, "installed": installed, "advice": advice})
    hints.sort(key=lambda h: -h["count"])

    inv = {
        "meta": {
            "tool_version": __version__,
            "date": datetime.now().isoformat(timespec="seconds"),
            "machine": loc.host if loc.remote else platform.node(),
            "user": (os.environ.get("USERNAME") or os.environ.get("USER") or "") if loc.current else loc.user,
            "domain": os.environ.get("USERDOMAIN", "") if loc.current else "",
            "os": "Windows (analyse à distance)" if loc.remote else platform.platform(),
            "windows": is_windows(),
            "home": loc.home,
            "profile_current": loc.current,
            "remote": loc.remote,
            "apps_read": bool(app_list),
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
        "installers": installer_info,
    }
    inv["advice"] = compute_advice(inv)
    return inv
