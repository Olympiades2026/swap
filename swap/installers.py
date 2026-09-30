"""Retrouve, sur le poste, les fichiers d'installation des applications qu'on ne peut pas réinstaller automatiquement.

Windows ne conserve pas l'installateur d'un logiciel installé : on ne peut donc récupérer que ceux qui traînent
dans vos dossiers (Téléchargements, C:\\Temp, C:\\Install…). Le rapprochement se fait par le nom du fichier.
"""

from __future__ import annotations

import os
import re
from typing import Optional

from .catalog import GENERIC_WORDS

EXTRA_GENERIC = {"driver", "client", "suite", "edition", "version", "setup", "installer", "install", "the", "pro", "for"}
INSTALL_SHARE_RE = re.compile(r"install|setup|logiciel|soft|applis?\b|deploy", re.I)


def _words(text: str) -> list:
    text = re.sub(r"[\d.]+", " ", text.lower())
    return [w for w in re.findall(r"[a-z0-9]+", text) if len(w) >= 4 and not w.isdigit()]


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def app_keywords(app: dict) -> tuple:
    """(mots propres à l'appli, mots de l'éditeur)."""
    vendor = set(_words(app.get("publisher", "")))
    generic = GENERIC_WORDS | EXTRA_GENERIC
    own = [w for w in _words(app["name"]) if w not in generic and w not in vendor]
    vend = [w for w in _words(app["name"]) if w in vendor]
    return own, list(vendor) if not own else vend


def match_installers(apps: list, candidates: list, stat=os.stat) -> dict:
    """{nom d'appli: [candidats triés du meilleur au moins bon]} ; chaque candidat = {path, size, score}."""
    normed = [(path, size, _norm(os.path.basename(path))) for path, size in candidates]
    result: dict = {}
    for app in apps:
        own, vend = app_keywords(app)
        if not own:
            continue
        need = 1 if len(own) <= 1 else 2
        found = []
        for path, size, name in normed:
            hits = sum(1 for w in own if w in name)
            if hits < need:
                continue
            score = hits * 2 + sum(1 for w in vend if w in name)
            try:
                mtime = stat(path).st_mtime
            except OSError:
                mtime = 0
            found.append({"path": path, "size": size, "score": score, "mtime": mtime})
        if found:
            found.sort(key=lambda c: (c["score"], c["mtime"], c["size"]), reverse=True)
            result[app["name"]] = found[:3]
    return result


def unmatched_installers(candidates: list, matched_paths: set, limit: int = 25) -> list:
    """Autres installateurs (.exe/.msi/.msix, gros .zip) trouvés, les plus gros d'abord, non rattachés à une appli."""
    def looks_like_installer(path: str, size: int) -> bool:
        ext = os.path.splitext(path)[1].lower()
        return ext in (".exe", ".msi", ".msix", ".msixbundle") or (ext in (".zip", ".7z") and size >= 50 * 1024 * 1024)  # gros zip = version portable

    other = [(p, s) for p, s in candidates if p not in matched_paths and looks_like_installer(p, s)]
    return [{"path": p, "size": s} for p, s in sorted(other, key=lambda x: -x[1])[:limit]]


def install_shares(drives: list) -> list:
    """Lecteurs réseau dont le chemin évoque un dépôt de logiciels (ex. \\\\serveur\\Install)."""
    return [d for d in drives if INSTALL_SHARE_RE.search(d.get("path", ""))]
