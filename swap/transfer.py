"""Copie des données vers un support, vérification, puis restauration sur le nouveau poste."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from datetime import datetime
from typing import Callable, Optional

from .fs import Excluder, make_excluder, walk
from .locations import Locations
from .model import Item
from .redirects import plan_redirects
from .util import human_size, is_windows, long_path

MANIFEST = "manifest.json"
PART = ".swap-part"
CHUNK = 1 << 20


# --- sélection ---------------------------------------------------------------------------------------------
def _match(item: Item, patterns) -> bool:
    return any(p.lower() in item.id.lower() or p.lower() in item.label.lower() for p in patterns)


def select_items(items: list, only=(), skip=(), interactive=False, input_fn=input, out=print) -> list:
    chosen = {it.id: it.default for it in items}
    if only:
        chosen = {it.id: _match(it, only) for it in items}
    for it in items:
        if skip and _match(it, skip):
            chosen[it.id] = False
    while interactive:
        out("")
        for n, it in enumerate(items, 1):
            size = human_size(it.size) if it.kind != "registry" else "registre"
            out(f" {n:3}. [{'x' if chosen[it.id] else ' '}] {it.label}  —  {size}{'  🔒' if it.sensitive else ''}")
        total = sum(it.size for it in items if chosen[it.id])
        out(f"\n Total sélectionné : {human_size(total)}")
        answer = input_fn(" Numéros à cocher/décocher (ex: 2 5 8), 'a' = tout, 'n' = rien, Entrée = valider : ").strip().lower()
        if not answer:
            break
        if answer in ("a", "n"):
            chosen = {it.id: answer == "a" for it in items}
            continue
        for tok in re.split(r"[\s,;]+", answer):
            if tok.isdigit() and 1 <= int(tok) <= len(items):
                key = items[int(tok) - 1].id
                chosen[key] = not chosen[key]
    return [it for it in items if chosen[it.id]]


# --- copie -------------------------------------------------------------------------------------------------
def _unchanged(dest: str, size: int, mtime: float) -> bool:
    try:
        st = os.stat(long_path(dest))
    except OSError:
        return False
    return st.st_size == size and abs(st.st_mtime - mtime) < 2


def copy_file(src: str, dest: str, want_hash: bool = False) -> Optional[str]:
    """Copie via un fichier temporaire (une interruption ne laisse jamais un fichier tronqué)."""
    dest_l, tmp = long_path(dest), long_path(dest + PART)
    os.makedirs(os.path.dirname(dest_l), exist_ok=True)
    digest = hashlib.sha256() if want_hash else None
    with open(long_path(src), "rb") as fin, open(tmp, "wb") as fout:
        while True:
            chunk = fin.read(CHUNK)
            if not chunk:
                break
            fout.write(chunk)
            if digest:
                digest.update(chunk)
    shutil.copystat(long_path(src), tmp)
    os.replace(tmp, dest_l)
    return digest.hexdigest() if digest else None


def file_hash(path: str) -> str:
    digest = hashlib.sha256()
    with open(long_path(path), "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _dest_path(base: str, rel: str) -> str:
    return os.path.join(base, *rel.split("/"))


def copy_item(item: Item, backup: str, *, dry_run=False, want_hash=False, progress: Optional[Callable] = None) -> dict:
    data_dir = os.path.join(backup, "data", item.id)
    result = {"copied": 0, "unchanged": 0, "cloud_skipped": 0, "bytes": 0, "errors": []}
    records = []

    def on_error(path, exc):
        result["errors"].append(f"{path} : {exc}")

    for e in walk(item.src, make_excluder(item.cache_excludes, item.extra_excludes, item.exclude_paths), on_error):
        dest = _dest_path(data_dir, e.rel)
        if e.kind == "d":
            if not dry_run:
                os.makedirs(long_path(dest), exist_ok=True)
            continue
        if e.placeholder:
            result["cloud_skipped"] += 1
            continue
        size, mtime = e.stat.st_size, e.stat.st_mtime
        digest = None
        try:
            if dry_run:
                result["copied"] += 1
            elif _unchanged(dest, size, mtime):
                result["unchanged"] += 1
                digest = file_hash(dest) if want_hash else None
            else:
                digest = copy_file(e.path, dest, want_hash)
                result["copied"] += 1
            result["bytes"] += size
        except OSError as exc:
            result["errors"].append(f"{e.path} : {exc}")
            continue
        rec = {"p": e.rel, "s": size, "m": mtime}
        if digest:
            rec["h"] = digest
        records.append(rec)
        if progress and (result["copied"] + result["unchanged"]) % 50 == 0:
            progress(f"{item.label} : {e.rel}")
    if not dry_run:
        os.makedirs(os.path.join(backup, "files"), exist_ok=True)
        with open(os.path.join(backup, "files", item.id + ".jsonl"), "w", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    result["files"] = len(records)
    return result


def export_registry(item: Item, backup: str, dry_run=False) -> dict:
    if not is_windows():
        return {"errors": ["export du registre impossible hors Windows"], "files": 0, "bytes": 0}
    out_dir = os.path.join(backup, "data", item.id)
    out_file = os.path.join(out_dir, "export.reg")
    if dry_run:
        return {"errors": [], "files": 1, "bytes": 0}
    os.makedirs(out_dir, exist_ok=True)
    proc = subprocess.run(["reg", "export", item.src, out_file, "/y"], capture_output=True, stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        return {"errors": [f"reg export {item.src} a échoué"], "files": 0, "bytes": 0}
    return {"errors": [], "files": 1, "bytes": os.path.getsize(out_file)}


def backup_dir_for(dest: str, machine: str) -> str:
    safe = re.sub(r"[^\w.\-]", "_", machine or "poste")
    return os.path.join(dest, f"SWAP-{safe}")


def check_space(items: list, dest: str) -> tuple:
    need = sum(it.size for it in items)
    try:
        free = shutil.disk_usage(dest if os.path.exists(dest) else os.path.dirname(os.path.abspath(dest))).free
    except OSError:
        free = -1
    return need, free


def run_backup(items: list, inv: dict, dest: str, *, dry_run=False, want_hash=False, progress: Optional[Callable] = None,
               redirects: Optional[list] = None) -> dict:
    backup = backup_dir_for(dest, inv["meta"]["machine"])
    if not dry_run:
        os.makedirs(backup, exist_ok=True)
    summary = {"backup": backup, "items": {}, "errors": []}
    for item in items:
        if item.kind == "registry":
            res = export_registry(item, backup, dry_run)
        else:
            res = copy_item(item, backup, dry_run=dry_run, want_hash=want_hash, progress=progress)
        summary["items"][item.id] = res
        summary["errors"] += res["errors"]
    if not dry_run:
        manifest = {
            "version": 1,
            "created": datetime.now().isoformat(timespec="seconds"),
            "machine": inv["meta"]["machine"],
            "user": inv["meta"]["user"],
            "hashed": want_hash,
            "redirects": [list(r) for r in (redirects or [])],
            "items": [it.to_dict() for it in items],
            "stats": {k: {kk: vv for kk, vv in v.items() if kk != "errors"} for k, v in summary["items"].items()},
        }
        with open(os.path.join(backup, MANIFEST), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False, indent=1)
        if summary["errors"]:
            with open(os.path.join(backup, "erreurs.log"), "w", encoding="utf-8") as fh:
                fh.write("\n".join(summary["errors"]) + "\n")
    return summary


# --- vérification ------------------------------------------------------------------------------------------
def load_manifest(backup: str) -> dict:
    with open(os.path.join(backup, MANIFEST), encoding="utf-8") as fh:
        return json.load(fh)


def verify_backup(backup: str, deep=False, progress: Optional[Callable] = None) -> dict:
    """Compare la sauvegarde à son manifeste : fichiers manquants, tailles, et empreintes si `deep`."""
    manifest = load_manifest(backup)
    problems, checked = [], 0
    for item in manifest["items"]:
        if item["kind"] == "registry":
            if not os.path.exists(os.path.join(backup, "data", item["id"], "export.reg")):
                problems.append(f"{item['id']} : export du registre absent")
            continue
        listing = os.path.join(backup, "files", item["id"] + ".jsonl")
        if not os.path.exists(listing):
            problems.append(f"{item['id']} : liste de fichiers absente")
            continue
        base = os.path.join(backup, "data", item["id"])
        with open(listing, encoding="utf-8") as fh:
            for line in fh:
                rec = json.loads(line)
                path = _dest_path(base, rec["p"])
                checked += 1
                try:
                    size = os.stat(long_path(path)).st_size
                except OSError:
                    problems.append(f"manquant : {item['id']}/{rec['p']}")
                    continue
                if size != rec["s"]:
                    problems.append(f"taille différente : {item['id']}/{rec['p']} ({size} au lieu de {rec['s']})")
                elif deep and rec.get("h") and file_hash(path) != rec["h"]:
                    problems.append(f"contenu différent : {item['id']}/{rec['p']}")
                if progress and checked % 200 == 0:
                    progress(f"{checked} fichiers vérifiés")
    return {"checked": checked, "problems": problems, "hashed": manifest.get("hashed", False)}


# --- restauration ------------------------------------------------------------------------------------------
def _restore_one(src: str, dest: str, overwrite: bool, dry_run: bool, res: dict) -> None:
    try:
        if os.path.exists(long_path(dest)) and not overwrite:
            res["skipped"] += 1
            return
        if not dry_run:
            copy_file(src, dest)
        res["restored"] += 1
    except OSError as exc:
        res["errors"].append(f"{dest} : {exc}")


def _restore_files(src_root: str, dest_root: str, overwrite: bool, dry_run: bool, res: dict) -> None:
    for e in walk(src_root, Excluder(files=["*" + PART])):
        dest = _dest_path(dest_root, e.rel)
        if e.kind == "d":
            if not dry_run:
                os.makedirs(long_path(dest), exist_ok=True)
            continue
        _restore_one(e.path, dest, overwrite, dry_run, res)


def run_restore(backup: str, loc: Locations, *, only=(), skip=(), overwrite=False, dry_run=False, interactive=False,
                input_fn=input, out=print, rules: Optional[list] = None) -> dict:
    manifest = load_manifest(backup)
    items = select_items([Item.from_dict(d) for d in manifest["items"]], only, skip, interactive, input_fn, out)
    # règles données maintenant d'abord (elles priment), puis celles enregistrées avec la sauvegarde
    all_rules = list(rules or []) + [tuple(r) for r in manifest.get("redirects", [])]
    redirected = plan_redirects(items, all_rules)
    summary = {"items": {}, "errors": []}
    for item in items:
        res = {"restored": 0, "skipped": 0, "errors": []}
        res["dest"] = redirected.get(item.id) or (loc.resolve(item.target) if item.kind != "registry" else "")
        data = os.path.join(backup, "data", item.id)
        if item.kind == "registry":
            reg = os.path.join(data, "export.reg")
            if os.path.exists(reg) and is_windows() and not dry_run:
                proc = subprocess.run(["reg", "import", reg], capture_output=True, stdin=subprocess.DEVNULL)
                if proc.returncode == 0:
                    res["restored"] = 1
                else:
                    res["errors"].append(f"reg import {reg} a échoué")
            else:
                res["restored"] = 1 if os.path.exists(reg) else 0
        elif item.kind == "file":
            dest = res["dest"]
            files = [f for f in os.listdir(data) if not f.endswith(PART)] if os.path.isdir(data) else []
            for name in files:
                _restore_one(os.path.join(data, name), dest, overwrite, dry_run, res)
        else:
            _restore_files(data, res["dest"], overwrite, dry_run, res)
        summary["items"][item.id] = res
        summary["errors"] += res["errors"]
    return summary
