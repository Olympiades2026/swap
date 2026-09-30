"""Génère le script PowerShell qui recrée lecteurs réseau, imprimantes, variables et applications."""

from __future__ import annotations

import re
from datetime import datetime


IP_PORT_RE = re.compile(r"^(?:IP_)?(\d+\.\d+\.\d+\.\d+)$")


def psq(text: str) -> str:
    """Chaîne PowerShell entre apostrophes (les apostrophes internes sont doublées)."""
    return "'" + str(text).replace("'", "''") + "'"


def build_setup_script(inv: dict) -> str:
    s = inv.get("system") or {}
    lines = [
        "# Script généré par swap le " + datetime.now().strftime("%Y-%m-%d %H:%M"),
        f"# Poste d'origine : {inv['meta']['machine']}",
        "# Relisez-le, mettez en commentaire (#) ce dont vous ne voulez pas, puis lancez-le dans PowerShell sur le NOUVEAU poste.",
        "$ErrorActionPreference = 'Continue'",
        "",
    ]
    if s.get("drives"):
        lines.append("# --- Lecteurs réseau ---")
        for d in s["drives"]:
            lines.append(f'net use {d["letter"]} "{d["path"]}" /persistent:yes')
        lines.append("")
    net_printers = [p for p in s.get("printers", []) if p["network_path"]]
    if net_printers:
        lines.append("# --- Imprimantes réseau ---")
        for p in net_printers:
            lines.append(f"Add-Printer -ConnectionName {psq(p['network_path'])}")
        default = next((p for p in net_printers if p["default"]), None)
        if default:
            lines.append(f"(New-Object -ComObject WScript.Network).SetDefaultPrinter({psq(default['network_path'])})")
        lines.append("")
    local = [p for p in s.get("printers", []) if not p["network_path"]]
    if local:
        lines.append("# --- Imprimantes locales / IP (le pilote indiqué doit être installé avant) ---")
        for p in local:
            m = IP_PORT_RE.match(p["port"] or "")
            if m:
                lines.append(f"Add-PrinterPort -Name {psq(p['port'])} -PrinterHostAddress {psq(m.group(1))} -ErrorAction SilentlyContinue")
                lines.append(f"Add-Printer -Name {psq(p['name'])} -DriverName {psq(p['driver'])} -PortName {psq(p['port'])}")
            else:
                lines.append(f"#   {p['name']}  (pilote : {p['driver']}, port : {p['port']}) : à recréer à la main")
        lines.append("")
    env = {k: v for k, v in (s.get("env") or {}).items() if k.upper() != "PATH" and not k.lower().startswith("onedrive")}
    path_value = next((v for k, v in (s.get("env") or {}).items() if k.upper() == "PATH"), "")
    extra_path = [p for p in path_value.split(";") if p.strip() and "windowsapps" not in p.lower()]
    if env or extra_path:
        lines.append("# --- Variables d'environnement utilisateur (OneDrive* exclues : propres à ce profil) ---")
        for k, v in env.items():
            lines.append(f"[Environment]::SetEnvironmentVariable({psq(k)}, {psq(v)}, 'User')")
        if extra_path:
            lines.append("$p = [Environment]::GetEnvironmentVariable('Path', 'User')")
            lines.append("foreach ($add in @(" + ", ".join(psq(x) for x in extra_path) + ")) { if (($p -split ';') -notcontains $add) { $p = $p.TrimEnd(';') + ';' + $add } }")
            lines.append("[Environment]::SetEnvironmentVariable('Path', $p, 'User')")
        lines.append("")
    real = [a for a in inv.get("apps", []) if not a.get("component")]
    auto = [a for a in real if a.get("winget_id")]
    manual = [a for a in real if not a.get("winget_id")]
    guessed = [a for a in manual if a.get("winget_guess")]
    manual = [a for a in manual if not a.get("winget_guess")]
    if auto:
        lines.append("# --- Applications installables automatiquement (winget) ---")
        for a in auto:
            ident = a["winget_id"]
            if re.fullmatch(r"[\w.\-+]+", ident):
                lines.append(f"winget install --id {ident} -e --accept-package-agreements --accept-source-agreements  # {a['name']}")
        lines.append("")
    if guessed:
        lines.append("# --- Suggestions winget (correspondance par nom, à relire avant de lancer) ---")
        for a in guessed:
            lines.append(f"winget install --id {a['winget_guess']} -e --accept-package-agreements --accept-source-agreements  # {a['name']}")
        lines.append("")
    if manual:
        lines.append("# --- À installer à la main (installateur + licence à prévoir) ---")
        lines += [f"#   {a['name']}{'' if a['version'] in a['name'] else ' ' + a['version']}  ({a['publisher']})".rstrip() for a in manual]
        lines.append("")
    return "\r\n".join(lines) + "\r\n"
