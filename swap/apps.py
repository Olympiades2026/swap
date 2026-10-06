"""Applications installées (registre Windows) et correspondance avec les paquets winget."""

from __future__ import annotations

import re

from .catalog import COMPONENT_RE, WINGET_GUESS
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
        title = " ".join(str(vals.get("DisplayName", "")).split())
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


def installed_apps(current_user: bool = True) -> list:
    if not is_windows():
        return []
    import winreg

    apps = []
    apps += _read_uninstall(winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY, "machine")
    apps += _read_uninstall(winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY, "machine")
    if current_user:  # les applications installées « pour l'utilisateur » d'un autre compte ne sont pas visibles d'ici
        apps += _read_uninstall(winreg.HKEY_CURRENT_USER, 0, "utilisateur")
    return _finalize(apps)


def _finalize(apps: list) -> list:
    seen, unique = set(), []
    for app in apps:
        ident = (app["name"].lower(), app["version"])
        if ident not in seen:
            seen.add(ident)
            # Les « applications web » Chrome/Edge (PWA) se recréent avec la synchronisation du navigateur.
            web_app = app["publisher"].replace("\\", "/").lower() in ("google/chrome", "microsoft/edge")
            app["component"] = web_app or bool(COMPONENT_RE.search(app["name"]))
            unique.append(app)
    return sorted(unique, key=lambda a: a["name"].lower())


# --- applications d'un AUTRE poste (lues à distance, sans rien y installer) ---------------------------------------
REMOTE_KEYS = (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall")


def parse_reg_query(text: str) -> list:
    """Sortie de `reg query <clé> /s` -> applications (même forme que la lecture locale du registre)."""
    apps, cur = [], {}

    def flush():
        title = " ".join(str(cur.get("DisplayName", "")).split())
        if (title and cur.get("SystemComponent") not in ("0x1", "1") and not cur.get("ParentKeyName") and not re.match(r"^KB\d+", title)
                and str(cur.get("ReleaseType", "")).lower() not in ("update", "hotfix", "security update")):
            apps.append({"name": title, "version": cur.get("DisplayVersion", ""), "publisher": cur.get("Publisher", ""),
                         "install_date": cur.get("InstallDate", ""), "scope": "machine"})

    for line in text.splitlines():
        if line.upper().startswith(("HKEY_", "\\\\")):
            flush()
            cur = {}
            continue
        m = re.match(r"^\s+(\w+)\s+REG_\w+\s*(.*)$", line)
        if m:
            cur[m.group(1)] = m.group(2).strip()
    flush()
    return apps


def remote_installed_apps(host: str) -> list:
    """Applications installées sur le PC `host`, lues par le registre distant puis, à défaut, par PowerShell à distance.

    Renvoie [] si aucun des deux n'est autorisé (service « Registre à distance » arrêté et WinRM désactivé)."""
    from .util import powershell_json

    apps = []
    for key in REMOTE_KEYS:
        apps += parse_reg_query(run(["reg", "query", "\\\\" + host + "\\HKLM\\" + key, "/s"], timeout=120))
    if not apps:
        keys = ",".join("'HKLM:\\" + k + "\\*'" for k in REMOTE_KEYS)
        script = ("Invoke-Command -ComputerName '" + host.replace("'", "''") + "' -ErrorAction SilentlyContinue -ScriptBlock { Get-ItemProperty "
                  + keys + " -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -and -not $_.SystemComponent -and -not $_.ParentKeyName } | "
                  "Select-Object DisplayName,DisplayVersion,Publisher,InstallDate } | ConvertTo-Json -Compress")
        for r in powershell_json(script, timeout=120):
            apps.append({"name": " ".join(str(r.get("DisplayName", "")).split()), "version": str(r.get("DisplayVersion") or ""),
                         "publisher": str(r.get("Publisher") or ""), "install_date": str(r.get("InstallDate") or ""), "scope": "machine"})
    return _finalize([a for a in apps if a["name"] and not re.match(r"^KB\d+", a["name"])])


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
        name, ident = " ".join(ln[: starts[1]].split()), ln[starts[1]:end].strip()
        if name and ident and " " not in ident:
            result[name] = ident
    return result


def guess_winget(name: str) -> str:
    """Suggestion d'identifiant winget pour une appli courante non reconnue automatiquement ('' si inconnue)."""
    for pattern, ident in WINGET_GUESS:
        if re.search(pattern, name, re.I):
            return ident
    return ""


def winget_map() -> dict:
    """{nom d'application: identifiant winget} pour ce qui est réinstallable automatiquement."""
    if not is_windows():
        return {}
    out = run(["winget", "list", "--source", "winget", "--accept-source-agreements", "--disable-interactivity"], timeout=180)
    return parse_winget_table(out)
