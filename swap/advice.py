"""Transforme l'inventaire en liste de conseils / points d'attention, classés par gravité."""

from __future__ import annotations

import os
import re

from .catalog import APP_ADVICE, GENERIC_ADVICE
from .util import human_size

GUID_CN_RE = re.compile(r"^CN=[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\s*$", re.I)
MDM_ISSUER_RE = re.compile(r"mdm|intune|sccm|device", re.I)
INSTALLER_EXTS = {".iso", ".msu", ".cab", ".img"}

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
    with_key = [c for c in sysinfo.get("certificates", []) if c.get("private_key")]
    managed = [c for c in with_key if GUID_CN_RE.match(c.get("subject", "")) or MDM_ISSUER_RE.search(c.get("issuer", ""))]
    personal = [c for c in with_key if c not in managed]
    if personal:
        advice.append({"level": "critique", "text": (
            f"{len(personal)} certificat(s) personnel(s) avec clé privée dans le magasin Windows (signature, authentification, S/MIME). "
            "Ils ne sont PAS copiés. Exportez-les en .pfx avec un mot de passe (certmgr.msc > Personnel > Toutes les tâches > Exporter) "
            "ou faites-en redélivrer de nouveaux ; s'ils sont non exportables, demandez-les à la DSI.")})
    if managed:
        advice.append({"level": "info", "text": (
            f"{len(managed)} certificat(s) d'appareil géré par l'entreprise (Intune/MDM) : rien à exporter, il est réémis automatiquement "
            "quand le nouveau poste est enrôlé.")})

    notable = inv.get("notable_files", {})
    if notable.get(".pst"):
        advice.append({"level": "important", "text": f"{len(notable['.pst'])} archive(s) Outlook (.pst) trouvée(s) : elles sont copiées avec les données mais à rattacher dans Outlook."})
    if notable.get(".kdbx"):
        advice.append({"level": "important", "text": "Base(s) KeePass (.kdbx) trouvée(s) : à copier et à ouvrir avec KeePass sur le nouveau poste."})
    if inv.get("big_files"):
        advice.append({"level": "info", "text": f"{len(inv['big_files'])} très gros fichier(s) (≥ 500 Mo) : machines virtuelles, ISO, archives... Vérifiez la place sur le support de copie."})

    installers = [b for b in inv.get("big_files", []) if os.path.splitext(b["path"])[1].lower() in INSTALLER_EXTS]
    if installers:
        total = sum(b["size"] for b in installers)
        advice.append({"level": "important", "text": (
            f"Au moins {human_size(total)} d'images disque et de mises à jour ({', '.join(sorted({os.path.splitext(b['path'])[1] for b in installers}))}) "
            "dans vos dossiers : elles se re-téléchargent. Pour ne pas les copier, ajoutez à la commande copy : --exclude *.iso *.msu *.cab")})

    inst = inv.get("installers") or {}
    if inst.get("matched"):
        advice.append({"level": "info", "text": (
            f"{len(inst['matched'])} installateur(s) retrouvé(s) sur ce poste pour des applications non installables automatiquement : "
            "ils sont copiés dans la sauvegarde (catégorie « Installateurs ») et lancés par le script de reconfiguration.")})
    if inst.get("missing"):
        advice.append({"level": "important", "text": (
            f"Aucun installateur retrouvé pour {len(inst['missing'])} application(s) ({', '.join(inst['missing'][:6])}"
            f"{'…' if len(inst['missing']) > 6 else ''}). Windows ne garde pas l'installateur d'un logiciel installé : "
            "il faudra le retélécharger chez l'éditeur ou le demander à la DSI (voir le tableau « Installateurs »).")})
    if inst.get("shares"):
        advice.append({"level": "info", "text": (
            "Le(s) lecteur(s) réseau " + ", ".join(f"{d['letter']} ({d['path']})" for d in inst["shares"]) +
            " semble(nt) être un dépôt de logiciels : cherchez-y les installateurs manquants ; le script de reconfiguration le remonte.")})

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
    if inv.get("meta", {}).get("windows") and inv.get("apps") and not inv.get("winget_available") and not inv.get("meta", {}).get("remote"):
        advice.append({"level": "info", "text": "winget n'est pas disponible sur ce poste : la liste des applications est à réinstaller à la main."})

    meta = inv.get("meta", {})
    if meta.get("remote"):
        text = (f"Analyse faite À DISTANCE depuis le serveur, sur le profil « {meta.get('user', '?')} » de {meta.get('machine', '?')} : "
                "les fichiers et la configuration des applications sont lus par le réseau. Imprimantes, lecteurs réseau, variables d'environnement, "
                "certificats personnels, identifiants Windows et réglages du registre ne sont pas lisibles à distance.")
        if not meta.get("apps_read", True):
            text += (" La liste des applications n'a pas pu être lue (service « Registre à distance » arrêté et WinRM désactivé sur ce PC) : "
                     "relevez-la à la main.")
        advice.append({"level": "important", "text": text})
    elif meta.get("profile_current") is False:
        advice.append({"level": "important", "text": (
            f"Analyse du profil « {meta.get('user', '?')} » faite depuis un autre compte : les fichiers et la configuration des "
            "applications sont bien listés, mais tout ce qui vit dans la session de cet utilisateur (clés de registre, lecteurs réseau, "
            "variables d'environnement, certificats personnels, identifiants Windows, applications installées « pour l'utilisateur ») "
            "n'est pas lisible d'ici. Pour l'inclure, refaites l'analyse en étant connecté avec ce compte.")})

    advice.sort(key=lambda a: ORDER[a["level"]])
    return advice
