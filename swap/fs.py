"""Parcours de dossiers : exclusions, détection des jonctions et des fichiers OneDrive en ligne seule."""

from __future__ import annotations

import fnmatch
import heapq
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Callable, Iterator, Optional

from .util import long_path

FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_SYSTEM = 0x4
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000

# Toujours ignorés : corbeille, verrous Office, fichiers temporaires, dossiers régénérables.
BASE_EXCLUDE_DIRS = {"$recycle.bin", "system volume information", "node_modules", "__pycache__", ".venv"}
BASE_EXCLUDE_FILES = ["thumbs.db", "desktop.ini", "~$*", "*.tmp", "*.swap-part", "hiberfil.sys", "pagefile.sys", "swapfile.sys"]
# Ignorés dans les dossiers de configuration d'applications (AppData).
CACHE_EXCLUDE_DIRS = {
    "cache", "cache2", "code cache", "gpucache", "grshadercache", "shadercache", "dawncache",
    "service worker", "crashpad", "cachestorage",
}

BIG_FILE = 500 * 1024 * 1024
# Extensions dont on liste toujours l'emplacement exact (fichiers faciles à oublier).
NOTABLE_EXTS = {".pst", ".kdbx", ".pfx", ".p12", ".ppk", ".ovpn", ".rdp", ".mdf", ".vhdx", ".vmdk", ".ova", ".wdp", ".wwp", ".wpp", ".fic"}
NOTABLE_CAP = 300

# Fichiers qui ressemblent à un installateur (pour retrouver ceux des applis à réinstaller à la main).
INSTALLER_EXTS = {".exe", ".msi", ".msix", ".msixbundle", ".appx", ".zip", ".7z", ".iso"}
INSTALLER_NAME_RE = re.compile(r"setup|install|instal|x64|x86|win64|win32|(64|32)[-_ ]?bit|amd64|update", re.I)
MIN_INSTALLER = 1024 * 1024
INSTALLER_CAP = 20000


class Excluder:
    def __init__(self, dirs=(), files=(), paths=()):
        self.dirs = {d.lower() for d in dirs}
        self.files = [f.lower() for f in files]
        self.paths = {os.path.normcase(os.path.abspath(p)) for p in paths}  # dossiers exclus par chemin complet

    def skip_path(self, path: str) -> bool:
        return bool(self.paths) and os.path.normcase(os.path.abspath(path)) in self.paths

    def skip_dir(self, name: str) -> bool:
        return name.lower() in self.dirs

    def skip_file(self, name: str) -> bool:
        low = name.lower()
        return any(fnmatch.fnmatchcase(low, pat) for pat in self.files)


def make_excluder(cache: bool = False, extra=(), paths=(), files=()) -> Excluder:
    dirs = set(BASE_EXCLUDE_DIRS) | {e.lower() for e in extra}
    if cache:
        dirs |= CACHE_EXCLUDE_DIRS
    return Excluder(dirs, list(BASE_EXCLUDE_FILES) + list(files), paths)


def attrs(st) -> int:
    return getattr(st, "st_file_attributes", 0)


def is_placeholder(st) -> bool:
    return bool(attrs(st) & (FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS))


def is_hidden_or_system(st) -> bool:
    return bool(attrs(st) & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM))


@dataclass
class Entry:
    kind: str  # "d" | "f"
    path: str
    rel: str  # chemin relatif à la racine, séparateur "/"
    stat: Optional[os.stat_result]
    placeholder: bool = False


def walk(root: str, excluder: Optional[Excluder] = None, on_error: Optional[Callable] = None) -> Iterator[Entry]:
    """Parcourt `root` sans suivre liens symboliques ni jonctions. Un fichier isolé est aussi accepté."""
    excluder = excluder or Excluder()
    try:
        if os.path.isfile(long_path(root)):
            st = os.stat(long_path(root))
            yield Entry("f", root, os.path.basename(root), st, is_placeholder(st))
            return
    except OSError as exc:
        if on_error:
            on_error(root, exc)
        return
    stack = [(root, "")]
    while stack:
        cur, rel = stack.pop()
        try:
            it = os.scandir(long_path(cur))
        except OSError as exc:
            if on_error:
                on_error(cur, exc)
            continue
        with it:
            found = []
            while True:
                try:
                    entry = next(it)
                except StopIteration:
                    break
                except OSError as exc:
                    if on_error:
                        on_error(cur, exc)
                    break
                try:
                    name = entry.name
                    st = entry.stat(follow_symlinks=False)
                    if entry.is_symlink() or attrs(st) & FILE_ATTRIBUTE_REPARSE_POINT:
                        continue
                    child = os.path.join(cur, name)
                    child_rel = f"{rel}/{name}" if rel else name
                    if entry.is_dir(follow_symlinks=False):
                        if not excluder.skip_dir(name) and not excluder.skip_path(child):
                            found.append(Entry("d", child, child_rel, st))
                    elif entry.is_file(follow_symlinks=False):
                        if not excluder.skip_file(name) and not excluder.skip_path(child):
                            found.append(Entry("f", child, child_rel, st, is_placeholder(st)))
                except OSError as exc:
                    if on_error:
                        on_error(os.path.join(cur, entry.name), exc)
        for e in found:
            yield e
            if e.kind == "d":
                stack.append((e.path, e.rel))


class TypeSink:
    """Collecte globale : types de fichiers, fichiers remarquables, plus gros fichiers."""

    def __init__(self, keep_big: int = 20, ignore: Optional[Callable] = None):
        self.ignore = ignore  # chemins à ne pas compter (ex. dossier d'installation d'un logiciel)
        self.exts: Counter = Counter()
        self.notable: dict = defaultdict(list)
        self.big: list = []
        self.installers: list = []  # [(chemin, taille)]
        self.keep_big = keep_big

    def add(self, path: str, size: int) -> None:
        if self.ignore and self.ignore(path):
            return
        ext = os.path.splitext(path)[1].lower()
        if ext:
            self.exts[ext] += 1
        if ext in NOTABLE_EXTS and len(self.notable[ext]) < NOTABLE_CAP:
            self.notable[ext].append(path)
        if ext in INSTALLER_EXTS and size >= MIN_INSTALLER and len(self.installers) < INSTALLER_CAP:
            # un .exe n'est retenu que si son nom évoque une installation (sinon ce serait l'appli elle-même)
            if ext != ".exe" or INSTALLER_NAME_RE.search(os.path.basename(path)):
                self.installers.append((path, size))
        if size >= BIG_FILE:
            item = (size, path)
            if len(self.big) < self.keep_big:
                heapq.heappush(self.big, item)
            else:
                heapq.heappushpop(self.big, item)


@dataclass
class Stats:
    size: int = 0
    files: int = 0
    placeholders: int = 0
    errors: int = 0


def collect(root: str, excluder: Excluder, sink: Optional[TypeSink] = None, progress: Optional[Callable] = None) -> Stats:
    stats = Stats()

    def on_error(_path, _exc):
        stats.errors += 1

    for e in walk(root, excluder, on_error):
        if e.kind != "f":
            continue
        if e.placeholder:
            stats.placeholders += 1
            continue
        stats.files += 1
        stats.size += e.stat.st_size
        if sink is not None:
            sink.add(e.path, e.stat.st_size)
        if progress and stats.files % 500 == 0:
            progress(root, stats)
    return stats
