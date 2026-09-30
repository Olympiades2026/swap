"""Petits utilitaires : plateforme, tailles, exécution de commandes."""

from __future__ import annotations

import json
import os
import re
import subprocess
import unicodedata


def is_windows() -> bool:
    return os.name == "nt"


def human_size(n: float) -> str:
    units = ["o", "Ko", "Mo", "Go", "To"]
    n = float(n)
    for unit in units:
        if n < 1024 or unit == units[-1]:
            return f"{int(n)} {unit}" if unit == "o" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} To"


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "x"


def long_path(path: str) -> str:
    """Préfixe `\\\\?\\` sous Windows pour dépasser la limite de 260 caractères."""
    if not is_windows():
        return path
    path = os.path.abspath(path)
    if path.startswith("\\\\?\\"):
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


def decode_output(raw: bytes) -> str:
    try:
        return raw.decode("utf-8").lstrip("﻿")
    except UnicodeDecodeError:
        pass
    if is_windows():
        try:
            import ctypes

            return raw.decode(f"cp{ctypes.windll.kernel32.GetOEMCP()}", errors="replace")
        except Exception:
            pass
    return raw.decode("latin-1", errors="replace")


def run(cmd: list[str], timeout: int = 60) -> str:
    """Exécute une commande et renvoie sa sortie standard ('' en cas d'échec)."""
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""
    return decode_output(proc.stdout)


def powershell(script: str, timeout: int = 90) -> str:
    if not is_windows():
        return ""
    prefix = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; $ProgressPreference='SilentlyContinue'; "
    return run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", prefix + script],
        timeout,
    )


def powershell_json(script: str, timeout: int = 90) -> list[dict]:
    out = powershell(script, timeout).strip()
    if not out:
        return []
    try:
        data = json.loads(out)
    except ValueError:
        return []
    if isinstance(data, dict):
        return [data]
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


def is_under(path: str, root: str) -> bool:
    p = os.path.normcase(os.path.abspath(path))
    r = os.path.normcase(os.path.abspath(root))
    return p == r or p.startswith(r.rstrip(os.sep) + os.sep)
