"""Copie des données vers un support, vérification, puis restauration sur le nouveau poste."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from typing import Callable, Optional

from .fs import Excluder, make_excluder, walk
from .locations import Locations
from .model import Item
from .redirects import plan_redirects
from .sink import CHUNK, PART, Cancelled, LocalSink, copy_file, file_hash  # noqa: F401  (réexportés)
from .util import human_size, is_windows, long_path

MANIFEST = "manifest.json"


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
def _dest_path(base: str, rel: str) -> str:
    return os.path.join(base, *rel.split("/"))


def copy_item(item: Item, sink, *, dry_run=False, want_hash=False, progress: Optional[Callable] = None,
              exclude_files=(), on_chunk: Optional[Callable[[int], None]] = None) -> dict:
    base = f"data/{item.id}"
    result = {"copied": 0, "unchanged": 0, "cloud_skipped": 0, "bytes": 0, "errors": []}
    records = []

    def on_error(path, exc):
        result["errors"].append(f"{path} : {exc}")

    for e in walk(item.src, make_excluder(item.cache_excludes, item.extra_excludes, item.exclude_paths, exclude_files), on_error):
        rel = f"{base}/{e.rel}"
        if e.kind == "d":
            if not dry_run:
                sink.makedirs(rel)
            continue
        if e.placeholder:
            result["cloud_skipped"] += 1
            continue
        size, mtime = e.stat.st_size, e.stat.st_mtime
        if on_chunk:
            on_chunk(0)  # point d'annulation même pour les petits fichiers
        digest = None
        try:
            if dry_run:
                result["copied"] += 1
            else:
                status, digest = sink.put_file(rel, e.path, size, mtime, want_hash, on_chunk)
                result[status] += 1
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
        sink.write_bytes(f"files/{item.id}.jsonl", "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records).encode("utf-8"))
    result["files"] = len(records)
    return result


def export_registry(item: Item, sink, dry_run=False) -> dict:
    if not is_windows():
        return {"errors": ["export du registre impossible hors Windows"], "files": 0, "bytes": 0}
    if dry_run:
        return {"errors": [], "files": 1, "bytes": 0}
    with tempfile.TemporaryDirectory() as tmp:
        out_file = os.path.join(tmp, "export.reg")
        proc = subprocess.run(["reg", "export", item.src, out_file, "/y"], capture_output=True, stdin=subprocess.DEVNULL)
        if proc.returncode != 0:
            return {"errors": [f"reg export {item.src} a échoué"], "files": 0, "bytes": 0}
        st = os.stat(out_file)
        sink.put_file(f"data/{item.id}/export.reg", out_file, st.st_size, st.st_mtime)
    return {"errors": [], "files": 1, "bytes": st.st_size}


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


def run_backup(items: list, inv: dict, dest: Optional[str] = None, *, sink=None, dry_run=False, want_hash=False,
               progress: Optional[Callable] = None, redirects: Optional[list] = None, exclude_files=(),
               on_chunk: Optional[Callable[[int], None]] = None) -> dict:
    """Copie les éléments vers `sink` (ou vers le dossier `dest`). `on_chunk(n)` reçoit les octets copiés (et peut lever Cancelled)."""
    if sink is None:
        sink = LocalSink(backup_dir_for(dest, inv["meta"]["machine"]))
    summary = {"backup": sink.describe(), "sink": sink, "items": {}, "errors": []}
    for item in items:
        if item.kind == "registry":
            res = export_registry(item, sink, dry_run)
        else:
            res = copy_item(item, sink, dry_run=dry_run, want_hash=want_hash, progress=progress, exclude_files=exclude_files,
                            on_chunk=on_chunk)
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
            "exclude_files": list(exclude_files),
            "items": [it.to_dict() for it in items],
            "stats": {k: {kk: vv for kk, vv in v.items() if kk != "errors"} for k, v in summary["items"].items()},
        }
        sink.write_bytes(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=1).encode("utf-8"))
        if summary["errors"]:
            sink.write_bytes("erreurs.log", ("\n".join(summary["errors"]) + "\n").encode("utf-8"))
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
def _restore_one(src: str, dest: str, overwrite: bool, dry_run: bool, res: dict, on_chunk=None) -> None:
    if on_chunk:
        on_chunk(0)  # point d'annulation
    try:
        if os.path.exists(long_path(dest)) and not overwrite:
            res["skipped"] += 1
            if on_chunk:
                try:
                    on_chunk(os.path.getsize(long_path(src)))
                except OSError:
                    pass
            return
        if not dry_run:
            copy_file(src, dest, False, on_chunk)
        res["restored"] += 1
    except OSError as exc:
        res["errors"].append(f"{dest} : {exc}")


def _restore_files(src_root: str, dest_root: str, overwrite: bool, dry_run: bool, res: dict, on_chunk=None) -> None:
    for e in walk(src_root, Excluder(files=["*" + PART])):
        dest = _dest_path(dest_root, e.rel)
        if e.kind == "d":
            if not dry_run:
                os.makedirs(long_path(dest), exist_ok=True)
            continue
        _restore_one(e.path, dest, overwrite, dry_run, res, on_chunk)


def run_restore(backup: str, loc: Locations, *, only=(), skip=(), overwrite=False, dry_run=False, interactive=False,
                input_fn=input, out=print, rules: Optional[list] = None, on_chunk=None) -> dict:
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
            if not getattr(loc, "current", True):
                if os.path.exists(reg):
                    res["errors"].append(
                        f"{item.label} : le registre ne peut être importé que pour le compte connecté. Ouvrez une session avec le "
                        f"compte « {loc.user} » et relancez la restauration (les fichiers déjà en place sont conservés).")
            elif os.path.exists(reg) and is_windows() and not dry_run:
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
                _restore_one(os.path.join(data, name), dest, overwrite, dry_run, res, on_chunk)
        else:
            _restore_files(data, res["dest"], overwrite, dry_run, res, on_chunk)
        summary["items"][item.id] = res
        summary["errors"] += res["errors"]
    return summary
