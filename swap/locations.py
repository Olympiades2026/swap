"""Où se trouvent les dossiers de l'utilisateur (Bureau, Documents... parfois déplacés vers OneDrive)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

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


@dataclass
class Locations:
    home: str
    appdata: str
    localappdata: str
    known: dict = field(default_factory=dict)

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

    def onedrive_roots(self) -> list:
        roots = []
        for var in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
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
