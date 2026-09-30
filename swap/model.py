"""Modèle de données : un « élément » est une chose à copier (dossier, fichier, clé de registre)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields


@dataclass
class Item:
    id: str
    label: str
    kind: str  # "dir" | "file" | "registry"
    src: str  # chemin source, ou clé de registre
    target: dict  # où remettre l'élément sur le nouveau poste (voir Locations.resolve)
    category: str = ""
    size: int = 0
    files: int = 0
    cloud_files: int = 0  # fichiers OneDrive « en ligne uniquement » (non copiés)
    default: bool = True
    sensitive: bool = False
    cache_excludes: bool = False
    extra_excludes: list = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Item":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})
