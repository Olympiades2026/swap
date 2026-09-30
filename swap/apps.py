"""Applications installées (registre Windows) et correspondance avec les paquets winget."""

from __future__ import annotations

import re

from .catalog import COMPONENT_RE
from .util import is_windows, run

UNINSTALL = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
FIELDS = ("DisplayName", "DisplayVersion", "Publisher", "InstallDate", "InstallLocation")


def _read_uninstall(hive, view, scope: str) -> list:
    import winreg

    apps = []
    try:
        key = winreg.OpenKey(hive, UNINSTALL, 0, winreg.KEY_READ | view)
    except OSError:
        return apps
    for i in range(winreg.QueryInfoKey(key)[0]):
        try:
            sub = winreg.OpenKey(key, winreg.EnumKey(key, i))
        except OSError:
            continue
        vals = {}
        for name in FIELDS + ("SystemComponent", "ParentKeyName", "ReleaseType"):
            try:
                vals[name] = winreg.QueryValueEx(sub, name)[0]
            except OSError:
                pass
        title = str(vals.get("DisplayName", "")).strip()
        if not title or vals.get("SystemComponent") == 1 or vals.get("ParentKeyName"):
            continue
        if str(vals.get("ReleaseType", "")).lower() in ("update", "hotfix", "security update") or re.match(r"^KB\d+", title):
            continue
        apps.append({
            "name": title,
            "version": str(vals.get("DisplayVersion", "") or ""),
            "publisher": str(vals.get("Publisher", "") or ""),
            "install_date": str(vals.get("InstallDate", "") or ""),
            "scope": scope,
        })
    return apps


def installed_apps() -> list:
    if not is_windows():
        return []
    import winreg

    apps = []
    apps += _read_uninstall(winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY, "machine")
    apps += _read_uninstall(winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY, "machine")
    apps += _read_uninstall(winreg.HKEY_CURRENT_USER, 0, "utilisateur")
    seen, unique = set(), []
    for app in apps:
        ident = (app["name"].lower(), app["version"])
        if ident not in seen:
            seen.add(ident)
            app["component"] = bool(COMPONENT_RE.search(app["name"]))
            unique.append(app)
    return sorted(unique, key=lambda a: a["name"].lower())


def parse_winget_table(text: str) -> dict:
    """Lit la sortie de `winget list` (tableau à colonnes) et renvoie {nom affiché: identifiant winget}."""
    lines = [ln.split("\r")[-1].rstrip() for ln in text.splitlines()]
    sep = next((i for i, ln in enumerate(lines) if re.fullmatch(r"-{10,}", ln.strip())), None)
    if sep is None or sep == 0:
        return {}
    starts = [m.start() for m in re.finditer(r"\S+(?: \S+)*", lines[sep - 1])]
    if len(starts) < 2:
        return {}
    end = starts[2] if len(starts) > 2 else None
    result = {}
    for ln in lines[sep + 1:]:
        if len(ln) <= starts[1]:
            continue
        name, ident = ln[: starts[1]].strip(), ln[starts[1]:end].strip()
        if name and ident and " " not in ident:
            result[name] = ident
    return result


def winget_map() -> dict:
    """{nom d'application: identifiant winget} pour ce qui est réinstallable automatiquement."""
    if not is_windows():
        return {}
    out = run(["winget", "list", "--source", "winget", "--accept-source-agreements", "--disable-interactivity"], timeout=180)
    return parse_winget_table(out)
