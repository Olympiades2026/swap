"""Transforme l'inventaire en liste de conseils / points d'attention, classés par gravité."""

from __future__ import annotations

import re

from .catalog import APP_ADVICE, GENERIC_ADVICE

ORDER = {"critique": 0, "important": 1, "info": 2}


def compute_advice(inv: dict) -> list:
    advice = [{"level": lvl, "text": txt} for lvl, txt in GENERIC_ADVICE]
    app_names = [a["name"] for a in inv.get("apps", []) if not a.get("component")]
    for pattern, level, text in APP_ADVICE:
        rx = re.compile(pattern, re.I)
        matches = [n for n in app_names if rx.search(n)]
        if matches:
            advice.append({"level": level, "text": f"{text}  (détecté : {', '.join(matches[:3])}{'…' if len(matches) > 3 else ''})"})

    sysinfo = inv.get("system", {})
    certs = [c for c in sysinfo.get("certificates", []) if c.get("private_key")]
    if certs:
        advice.append({"level": "critique", "text": (
            f"{len(certs)} certificat(s) personnel(s) avec clé privée dans le magasin Windows (signature, authentification, S/MIME). "
            "Ils ne sont PAS copiés. Exportez-les en .pfx avec un mot de passe (certmgr.msc > Personnel > Toutes les tâches > Exporter) "
            "ou faites-en redélivrer de nouveaux ; s'ils sont non exportables, demandez-les à la DSI.")})

    notable = inv.get("notable_files", {})
    if notable.get(".pst"):
        advice.append({"level": "important", "text": f"{len(notable['.pst'])} archive(s) Outlook (.pst) trouvée(s) : elles sont copiées avec les données mais à rattacher dans Outlook."})
    if notable.get(".kdbx"):
        advice.append({"level": "important", "text": "Base(s) KeePass (.kdbx) trouvée(s) : à copier et à ouvrir avec KeePass sur le nouveau poste."})
    if inv.get("big_files"):
        advice.append({"level": "info", "text": f"{len(inv['big_files'])} très gros fichier(s) (≥ 500 Mo) : machines virtuelles, ISO, archives... Vérifiez la place sur le support de copie."})

    items = inv.get("items", [])
    drives = [i for i in items if i["category"] == "Autres disques" or i["category"].startswith("Dossiers hors profil")]
    if drives:
        advice.append({"level": "important", "text": (
            f"{len(drives)} dossier(s) hors du profil utilisateur (racine des disques) : c'est souvent là que se cachent les données "
            "métier. Vérifiez chacun d'eux dans la liste.")})
    if any(i.get("sensitive") for i in items if i.get("default")):
        advice.append({"level": "important", "text": "Certaines données à copier sont sensibles (clés SSH, mots de passe de logiciels FTP...) : utilisez un disque chiffré (BitLocker To Go) et effacez-le ensuite."})

    cloud = sum(i.get("cloud_files", 0) for i in items)
    if cloud:
        advice.append({"level": "info", "text": f"{cloud} fichier(s) OneDrive « en ligne uniquement » ne sont pas copiés (ils ne sont pas sur le disque) ; ils réapparaîtront avec la synchronisation."})

    if sysinfo.get("credentials"):
        advice.append({"level": "info", "text": f"{len(sysinfo['credentials'])} identifiant(s) enregistré(s) dans le Gestionnaire d'identifiants Windows (partages, sites...) : à ressaisir sur le nouveau poste."})
    if inv.get("meta", {}).get("windows") and inv.get("apps") and not inv.get("winget_available"):
        advice.append({"level": "info", "text": "winget n'est pas disponible sur ce poste : la liste des applications est à réinstaller à la main."})

    advice.sort(key=lambda a: ORDER[a["level"]])
    return advice
