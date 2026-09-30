"""Configuration système à recréer sur le nouveau poste (imprimantes, lecteurs réseau, tâches...)."""

from __future__ import annotations

import os
import re

from .util import is_windows, powershell_json, run

VIRTUAL_PRINTER_RE = re.compile(r"pdf|xps|onenote|fax|microsoft print|send to|anydesk|snagit|teams|webex", re.I)


def printers() -> list:
    rows = powershell_json(
        "Get-CimInstance Win32_Printer | Select-Object Name,DriverName,PortName,Network,Default,ServerName,ShareName "
        "| ConvertTo-Json -Compress"
    )
    result = []
    for r in rows:
        name = str(r.get("Name") or "")
        if not name or VIRTUAL_PRINTER_RE.search(name):
            continue
        path = ""
        if r.get("Network") and r.get("ShareName"):
            path = "\\\\" + str(r.get("ServerName") or "").lstrip("\\") + "\\" + str(r["ShareName"])
        result.append({
            "name": name,
            "driver": r.get("DriverName") or "",
            "port": r.get("PortName") or "",
            "network_path": path,
            "default": bool(r.get("Default")),
        })
    return result


def mapped_drives() -> list:
    if not is_windows():
        return []
    import winreg

    drives = []
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Network")
    except OSError:
        return drives
    for i in range(winreg.QueryInfoKey(key)[0]):
        letter = winreg.EnumKey(key, i)
        try:
            sub = winreg.OpenKey(key, letter)
            remote = winreg.QueryValueEx(sub, "RemotePath")[0]
        except OSError:
            continue
        try:
            user = winreg.QueryValueEx(sub, "UserName")[0]
        except OSError:
            user = ""
        drives.append({"letter": letter.upper() + ":", "path": remote, "user": user})
    return drives


def wifi_profiles() -> list:
    out = run(["netsh", "wlan", "show", "profiles"]) if is_windows() else ""
    names = []
    for line in out.splitlines():
        if " : " in line:
            value = line.split(" : ", 1)[1].strip()
            if value and value not in names:
                names.append(value)
    return names


def _run_entries() -> list:
    if not is_windows():
        return []
    import winreg

    entries = []
    for hive, scope in ((winreg.HKEY_CURRENT_USER, "utilisateur"), (winreg.HKEY_LOCAL_MACHINE, "machine")):
        try:
            key = winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\Run")
        except OSError:
            continue
        for i in range(winreg.QueryInfoKey(key)[1]):
            try:
                name, value, _ = winreg.EnumValue(key, i)
            except OSError:
                break
            entries.append({"name": name, "command": str(value), "scope": scope})
    return entries


def startup(appdata: str) -> dict:
    folder = os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
    try:
        files = sorted(f for f in os.listdir(folder) if f.lower() != "desktop.ini")
    except OSError:
        files = []
    return {"registry": _run_entries(), "folder": files}


def scheduled_tasks() -> list:
    rows = powershell_json(
        "Get-ScheduledTask | Where-Object {$_.TaskPath -notlike '\\Microsoft\\*'} | Select-Object TaskName,TaskPath,State,"
        "@{n='Action';e={($_.Actions | ForEach-Object {$_.Execute + ' ' + $_.Arguments}) -join '; '}} | ConvertTo-Json -Compress"
    )
    return [
        {"name": r.get("TaskName", ""), "path": r.get("TaskPath", ""), "state": str(r.get("State", "")), "action": r.get("Action", "")}
        for r in rows
    ]


def odbc_sources() -> list:
    if not is_windows():
        return []
    import winreg

    sources = []
    for hive, scope in ((winreg.HKEY_CURRENT_USER, "utilisateur"), (winreg.HKEY_LOCAL_MACHINE, "machine")):
        try:
            key = winreg.OpenKey(hive, r"Software\ODBC\ODBC.INI\ODBC Data Sources")
        except OSError:
            continue
        for i in range(winreg.QueryInfoKey(key)[1]):
            try:
                name, driver, _ = winreg.EnumValue(key, i)
            except OSError:
                break
            sources.append({"name": name, "driver": str(driver), "scope": scope})
    return sources


def env_vars() -> dict:
    if not is_windows():
        return {}
    import winreg

    result = {}
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment")
    except OSError:
        return result
    for i in range(winreg.QueryInfoKey(key)[1]):
        try:
            name, value, _ = winreg.EnumValue(key, i)
        except OSError:
            break
        if name.upper() not in ("TEMP", "TMP"):
            result[name] = str(value)
    return result


def hosts_entries() -> list:
    path = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "drivers", "etc", "hosts")
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return [ln.strip() for ln in fh if ln.strip() and not ln.lstrip().startswith("#")]
    except OSError:
        return []


def personal_certificates() -> list:
    rows = powershell_json(
        r"Get-ChildItem Cert:\CurrentUser\My | Select-Object Subject,Thumbprint,HasPrivateKey,FriendlyName,"
        r"@{n='NotAfter';e={$_.NotAfter.ToString('yyyy-MM-dd')}} | ConvertTo-Json -Compress"
    )
    return [
        {"subject": r.get("Subject", ""), "thumbprint": r.get("Thumbprint", ""), "private_key": bool(r.get("HasPrivateKey")),
         "expires": r.get("NotAfter", ""), "name": r.get("FriendlyName") or ""}
        for r in rows
    ]


def stored_credentials() -> list:
    """Noms des identifiants enregistrés dans Windows (jamais les mots de passe)."""
    out = run(["cmdkey", "/list"]) if is_windows() else ""
    targets = []
    for line in out.splitlines():
        m = re.match(r"\s*(?:Target|Cible)\s*:\s*(.+)", line, re.I)
        if m:
            targets.append(m.group(1).strip())
    return targets


def collect_all(appdata: str) -> dict:
    return {
        "printers": printers(),
        "drives": mapped_drives(),
        "wifi": wifi_profiles(),
        "startup": startup(appdata),
        "tasks": scheduled_tasks(),
        "odbc": odbc_sources(),
        "env": env_vars(),
        "hosts": hosts_entries() if is_windows() else [],
        "certificates": personal_certificates(),
        "credentials": stored_credentials(),
    }
