"""Où se trouvent les dossiers de l'utilisateur (Bureau, Documents... parfois déplacés vers OneDrive)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime

from .util import is_under, is_windows

# nom logique -> (libellé, valeur de la clé de registre « Shell Folders », dossier par défaut)
KNOWN_FOLDERS = {
    "Desktop": ("Bureau", "Desktop", "Desktop"),
    "Documents": ("Documents", "Personal", "Documents"),
    "Downloads": ("Téléchargements", "{374DE290-123F-4565-9164-91E5B7DA0FF3}", "Downloads"),
    "Pictures": ("Images", "My Pictures", "Pictures"),
    "Videos": ("Vidéos", "My Video", "Videos"),
    "Music": ("Musique", "My Music", "Music"),
}


def _registry_known_folders() -> dict:
    found: dict = {}
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders"
        )
        for name, (_label, reg_name, _default) in KNOWN_FOLDERS.items():
            try:
                value = winreg.QueryValueEx(key, reg_name)[0]
                if value:
                    found[name] = os.path.expandvars(value)
            except OSError:
                pass
    except (ImportError, OSError):
        pass
    return found


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def _is_empty_dir(path: str) -> bool:
    """Vrai si le dossier n'existe pas ou ne contient rien (cas d'un dossier redirigé vers OneDrive)."""
    try:
        with os.scandir(path) as it:
            return next(it, None) is None
    except OSError:
        return True


def _redirected_to_onedrive(home: str, default: str) -> str:
    """`<profil>\\OneDrive*\\Documents` s'il existe (le dossier du profil est alors vide), sinon ''."""
    try:
        for name in sorted(os.listdir(home)):
            candidate = os.path.join(home, name, default)
            if name.lower().startswith("onedrive") and os.path.isdir(candidate):
                return candidate
    except OSError:
        pass
    return ""


# comptes qui ne sont pas de vrais utilisateurs
_SYSTEM_PROFILES = {"public", "default", "default user", "all users", "defaultuser0", "wdagutilityaccount", "desktop.ini"}


def users_dir() -> str:
    if is_windows():
        return os.path.join(os.environ.get("SystemDrive", "C:") + os.sep, "Users")
    return "/home"


def list_profiles(base: str = "", connected: bool = True) -> list:
    """Profils utilisateurs présents sur ce poste, le compte connecté en premier puis du plus récent au plus ancien.

    Chaque entrée : {name, path, current, last_used ('AAAA-MM-JJ' ou '')}.
    `connected=False` : on liste les profils d'un AUTRE poste (`base` = \\\\PC\\C$\\Users) : aucun n'est « le compte connecté ».
    """
    base = base or users_dir()
    mine = os.path.expanduser("~")
    profiles = []
    try:
        names = os.listdir(base)
    except OSError:
        names = []
    for name in names:
        path = os.path.join(base, name)
        if name.lower() in _SYSTEM_PROFILES or not os.path.isdir(path) or os.path.islink(path):
            continue
        hive = os.path.join(path, "NTUSER.DAT")
        if is_windows() and not (os.path.exists(hive) or os.path.isdir(os.path.join(path, "AppData"))):
            continue  # dossier quelconque dans C:\Users, pas un profil
        try:
            stamp = os.stat(hive if os.path.exists(hive) else path).st_mtime
            last = datetime.fromtimestamp(stamp).strftime("%Y-%m-%d")
        except OSError:
            stamp, last = 0.0, ""
        profiles.append({"name": name, "path": path, "current": connected and _same_path(path, mine), "last_used": last, "_t": stamp})
    if connected and not any(p["current"] for p in profiles) and os.path.isdir(mine):
        # profil du compte connecté hors du dossier des profils (redirigé, autre disque...) : il doit toujours être proposé
        profiles.append({"name": os.path.basename(mine.rstrip("\\/")), "path": mine, "current": True,
                         "last_used": datetime.now().strftime("%Y-%m-%d"), "_t": float("inf")})
    profiles.sort(key=lambda p: (not p["current"], -p["_t"], p["name"].lower()))
    for p in profiles:
        del p["_t"]
    return profiles


def find_profile(name_or_path: str, base: str = "") -> str:
    """Chemin du profil désigné par un nom (« jdupont ») ou un chemin. Lève ValueError s'il n'existe pas."""
    value = (name_or_path or "").strip().strip('"')
    if os.path.isabs(value) and os.path.isdir(value):
        return value
    for p in list_profiles(base):
        if p["name"].lower() == value.lower() or p["name"].lower().endswith("\\" + value.lower()):
            return p["path"]
    known = ", ".join(p["name"] for p in list_profiles(base)) or "aucun"
    raise ValueError(f"Profil « {value} » introuvable. Profils présents : {known}.")


def profile_label(p: dict) -> str:
    """Texte lisible pour une liste déroulante."""
    extra = "compte connecté" if p["current"] else (f"dernière utilisation {p['last_used']}" if p["last_used"] else "")
    return f"{p['name']}  ({extra})" if extra else p["name"]


@dataclass
class Locations:
    home: str
    appdata: str
    localappdata: str
    known: dict = field(default_factory=dict)
    current: bool = True  # False : profil d'un autre compte (registre, certificats... du compte connecté inutilisables)

    @property
    def user(self) -> str:
        return os.path.basename(self.home.rstrip("\\/"))

    @classmethod
    def detect(cls) -> "Locations":
        home = os.path.expanduser("~")
        if is_windows():
            appdata = os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
            local = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        else:
            appdata = os.path.join(home, ".config")
            local = os.path.join(home, ".local", "share")
        known = {name: os.path.join(home, default) for name, (_l, _r, default) in KNOWN_FOLDERS.items()}
        if is_windows():
            known.update(_registry_known_folders())
        return cls(home, appdata, local, known)

    @classmethod
    def for_profile(cls, home: str) -> "Locations":
        """Emplacements du profil `home` (C:\\Users\\xxx). Le compte connecté garde la détection complète (registre)."""
        mine = cls.detect()
        if _same_path(home, mine.home):
            return mine
        if is_windows():
            appdata, local = os.path.join(home, "AppData", "Roaming"), os.path.join(home, "AppData", "Local")
        else:
            appdata, local = os.path.join(home, ".config"), os.path.join(home, ".local", "share")
        known = {}
        for name, (_l, _r, default) in KNOWN_FOLDERS.items():
            plain = os.path.join(home, default)
            known[name] = _redirected_to_onedrive(home, default) if _is_empty_dir(plain) else plain
            known[name] = known[name] or plain
        return cls(home, appdata, local, known, current=False)

    @classmethod
    def for_remote(cls, host: str, user: str, users_base: str = "", drive_root=None) -> "RemoteLocations":
        """Profil `user` d'un AUTRE poste, atteint par ses partages d'administration (\\\\PC\\C$...).

        `users_base` et `drive_root` ne servent qu'aux tests (valeurs par défaut : \\\\PC\\C$\\Users et \\\\PC\\X$)."""
        root = drive_root or (lambda drive: "\\\\" + host.strip().strip("\\") + "\\" + drive.upper().rstrip(":") + "$")
        base = users_base or os.path.join(root("C"), "Users")
        home = os.path.join(base, user)
        base_loc = cls.for_profile(home)
        return RemoteLocations(home, os.path.join(home, "AppData", "Roaming"), os.path.join(home, "AppData", "Local"),
                               base_loc.known, current=False, host=host, drive_root=root)

    def onedrive_roots(self) -> list:
        roots = []
        for var in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer") if self.current else ():
            value = os.environ.get(var)
            if value and value not in roots:
                roots.append(value)
        try:
            for name in os.listdir(self.home):
                full = os.path.join(self.home, name)
                if name.lower().startswith("onedrive") and os.path.isdir(full) and full not in roots:
                    roots.append(full)
        except OSError:
            pass
        return roots

    def in_onedrive(self, path: str) -> bool:
        return any(is_under(path, root) for root in self.onedrive_roots())

    def target_for(self, path: str) -> dict:
        """Décrit `path` de façon portable (relatif à Documents, au profil...) pour le retrouver sur un autre poste."""
        best = None
        for name, base in self.known.items():
            if is_under(path, base) and (best is None or len(base) > len(best[1])):
                best = (name, base)
        if best:
            rel = os.path.relpath(path, best[1]).replace(os.sep, "/")
            return {"kind": "known", "name": best[0], "rel": "" if rel == "." else rel}
        if is_under(path, self.home):
            return {"kind": "home", "rel": os.path.relpath(path, self.home).replace(os.sep, "/")}
        return {"kind": "abs", "path": path}

    def resolve(self, target: dict) -> str:
        """Transforme une cible enregistrée dans la sauvegarde en chemin réel sur CE poste."""
        kind = target["kind"]
        rel = os.path.join(*target["rel"].split("/")) if target.get("rel") else ""
        if kind == "known":
            base = self.known.get(target["name"]) or os.path.join(self.home, target["name"])
            return os.path.join(base, rel) if rel else base
        if kind == "appdata":
            return os.path.join(self.appdata, rel)
        if kind == "localappdata":
            return os.path.join(self.localappdata, rel)
        if kind == "home":
            return os.path.join(self.home, rel)
        if kind == "abs":
            path = target["path"]
            drive = os.path.splitdrive(path)[0]
            if is_windows() and drive and not os.path.exists(drive + os.sep):
                # Le disque d'origine n'existe pas sur ce poste : on range sous le profil.
                tail = path[len(drive):].lstrip("\\/")
                return os.path.join(self.home, "Migration_" + drive.rstrip(":"), tail)
            return path
        raise ValueError(f"cible inconnue : {target!r}")


@dataclass
class RemoteLocations(Locations):
    """Emplacements sur un autre poste : les chemins « C:\\Mes Projets » deviennent « \\\\PC\\C$\\Mes Projets »."""

    host: str = ""
    drive_root: object = None  # fonction lettre de lecteur -> racine accessible depuis ce poste

    def remote_path(self, path: str) -> str:
        """Chemin d'un lecteur du poste distant tel qu'on l'atteint d'ici. Les chemins sans lecteur sont rangés sous le profil."""
        path = (path or "").strip()
        if path.startswith("\\\\"):
            return path
        m = re.match(r"^([A-Za-z]):[\\/]*(.*)$", path)
        if not m:
            return os.path.join(self.home, path) if path else self.home
        rest = [part for part in re.split(r"[\\/]+", m.group(2)) if part]
        return os.path.join(self.drive_root(m.group(1)), *rest)

    def resolve(self, target: dict) -> str:
        if target.get("kind") == "abs":
            return self.remote_path(target["path"])
        return super().resolve(target)
