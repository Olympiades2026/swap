"""Destinations d'une copie : dossier local / partage Windows (LocalSink) ou PC distant (voir net.py).

Une destination reçoit des chemins relatifs à la racine de la sauvegarde (séparateur « / »).
"""

from __future__ import annotations

import hashlib
import os
import shutil
from typing import Callable, Optional

from .util import long_path

PART = ".swap-part"
CHUNK = 1 << 20


class Cancelled(Exception):
    """L'utilisateur a annulé l'opération en cours."""


class SinkError(Exception):
    """Erreur fatale de la destination (connexion perdue...) : contrairement à un OSError, elle arrête toute la copie."""


def file_hash(path: str) -> str:
    digest = hashlib.sha256()
    with open(long_path(path), "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_file(src: str, dest: str, want_hash: bool = False, on_chunk: Optional[Callable[[int], None]] = None) -> Optional[str]:
    """Copie via un fichier temporaire (une interruption ne laisse jamais un fichier tronqué)."""
    dest_l, tmp = long_path(dest), long_path(dest + PART)
    os.makedirs(os.path.dirname(dest_l), exist_ok=True)
    digest = hashlib.sha256() if want_hash else None
    try:
        with open(long_path(src), "rb") as fin, open(tmp, "wb") as fout:
            while True:
                chunk = fin.read(CHUNK)
                if not chunk:
                    break
                fout.write(chunk)
                if digest:
                    digest.update(chunk)
                if on_chunk:
                    on_chunk(len(chunk))
        shutil.copystat(long_path(src), tmp)
        os.replace(tmp, dest_l)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return digest.hexdigest() if digest else None


def is_current(dest: str, size: int, mtime: float) -> bool:
    """Le fichier de destination a déjà la bonne taille et la bonne date (copie incrémentale)."""
    try:
        st = os.stat(long_path(dest))
    except OSError:
        return False
    return st.st_size == size and abs(st.st_mtime - mtime) < 2


class LocalSink:
    """Écrit dans un dossier (disque externe, dossier local ou partage réseau \\\\serveur\\partage)."""

    def __init__(self, root: str):
        self.root = root

    def describe(self) -> str:
        return self.root

    def path(self, rel: str) -> str:
        return os.path.join(self.root, *rel.split("/"))

    def makedirs(self, rel: str) -> None:
        os.makedirs(long_path(self.path(rel)), exist_ok=True)

    def put_file(self, rel: str, src: str, size: int, mtime: float, want_hash: bool = False,
                 on_chunk: Optional[Callable[[int], None]] = None) -> tuple:
        """Renvoie ("copied" | "unchanged", empreinte ou None)."""
        dest = self.path(rel)
        if is_current(dest, size, mtime):
            if on_chunk:
                on_chunk(size)
            return "unchanged", (file_hash(dest) if want_hash else None)
        return "copied", copy_file(src, dest, want_hash, on_chunk)

    def write_bytes(self, rel: str, data: bytes) -> None:
        dest = self.path(rel)
        os.makedirs(os.path.dirname(long_path(dest)), exist_ok=True)
        tmp = long_path(dest + PART)
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, long_path(dest))

    def put_tree(self, local_dir: str, prefix: str = "") -> int:
        """Envoie le contenu d'un dossier local (rapports, outil, lanceur) à la racine de la sauvegarde."""
        count = 0
        for folder, _dirs, files in os.walk(local_dir):
            for name in files:
                full = os.path.join(folder, name)
                rel = os.path.relpath(full, local_dir).replace(os.sep, "/")
                st = os.stat(full)
                self.put_file(prefix + rel, full, st.st_size, st.st_mtime)
                count += 1
        return count

    def close(self) -> None:
        pass
