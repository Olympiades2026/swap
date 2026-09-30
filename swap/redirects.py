"""Règles de destination : « ce qui correspond à ce motif arrive à cet endroit sur le nouveau poste ».

Format d'une règle :   motif = C:\\Dossier\\Destination
Le motif est cherché (sans tenir compte des majuscules) dans l'identifiant, le nom et le chemin d'origine de
l'élément ; `a|b` signifie « a ou b ». La première règle qui correspond l'emporte.
"""

from __future__ import annotations

import os
from typing import Iterable, Optional

from .model import Item

TEMPLATE = """# Règles de destination pour la restauration sur le nouveau poste.
# Une règle par ligne :   motif = dossier de destination
# - le motif est cherché dans l'identifiant, le nom et le chemin d'origine de chaque élément (voir rapport.html)
# - plusieurs motifs possibles avec | (ex. projet|donnees)
# - si une règle correspond à PLUSIEURS éléments, chacun est rangé dans un sous-dossier portant son nom
# - la première règle qui correspond l'emporte
#
# Exemples (retirez le # pour les activer) :
# pcsoft-projet = C:\\Mes Projets
# pcsoft-donnees = D:\\Donnees HFSQL
# Bureau = C:\\Users\\Public\\Desktop
"""


def parse_rule(text: str) -> Optional[tuple]:
    text = text.strip()
    if not text or text.startswith("#") or "=" not in text:
        return None
    pattern, dest = (part.strip().strip('"') for part in text.split("=", 1))
    return (pattern, dest) if pattern and dest else None


def parse_rules(lines: Iterable[str]) -> list:
    return [r for r in (parse_rule(ln) for ln in lines) if r]


def load_rules_file(path: str) -> list:
    try:
        with open(path, encoding="utf-8-sig") as fh:
            return parse_rules(fh)
    except OSError:
        return []


def _haystack(item: Item) -> str:
    return f"{item.id} {item.label} {item.src}".lower()


def matching_rule(item: Item, rules: list) -> Optional[int]:
    if item.kind == "registry":
        return None
    hay = _haystack(item)
    for index, (pattern, _dest) in enumerate(rules):
        if any(alt.strip().lower() in hay for alt in pattern.split("|") if alt.strip()):
            return index
    return None


def plan_redirects(items: list, rules: list) -> dict:
    """{item.id: dossier de destination} pour les éléments concernés par une règle."""
    by_rule: dict = {}
    for item in items:
        idx = matching_rule(item, rules)
        if idx is not None:
            by_rule.setdefault(idx, []).append(item)
    result = {}
    for idx, group in by_rule.items():
        dest = rules[idx][1]
        for item in group:
            name = os.path.basename(item.src.rstrip("\\/")) or item.id
            if item.kind == "file":
                result[item.id] = os.path.join(dest, name)
            else:
                result[item.id] = os.path.join(dest, name) if len(group) > 1 else dest
    return result
