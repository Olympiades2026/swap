"""Connaissances « métier » : configurations d'applis à emporter, conseils par logiciel, types de fichiers."""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class AppConfig:
    id: str
    label: str
    paths: list = field(default_factory=list)  # [(racine, chemin relatif avec "/")] racine: appdata|localappdata|home
    registry: list = field(default_factory=list)  # clés HKCU à exporter
    cache: bool = False  # ignorer les dossiers de cache
    sensitive: bool = False
    default: bool = True
    extra_excludes: list = field(default_factory=list)
    note: str = ""


APP_CONFIGS = [
    AppConfig("firefox", "Firefox (profils, favoris, extensions)", [("appdata", "Mozilla/Firefox")], cache=True,
              note="Mots de passe inclus dans le profil ; le plus simple reste la synchronisation Firefox."),
    AppConfig("thunderbird", "Thunderbird (comptes, courriers locaux)", [("appdata", "Thunderbird")], cache=True),
    AppConfig("chrome-favoris", "Chrome : favoris", [("localappdata", "Google/Chrome/User Data/Default/Bookmarks")],
              note="Mots de passe, historique, extensions : activer la synchronisation du compte Google sur l'ancien poste."),
    AppConfig("edge-favoris", "Edge : favoris", [("localappdata", "Microsoft/Edge/User Data/Default/Bookmarks")],
              note="Mots de passe, historique : activer la synchronisation du compte Microsoft."),
    AppConfig("outlook-signatures", "Outlook : signatures", [("appdata", "Microsoft/Signatures")]),
    AppConfig("outlook-autocompletion", "Outlook : adresses de saisie semi-automatique",
              [("localappdata", "Microsoft/Outlook/RoamCache")],
              note="Peut demander que le profil Outlook porte le même nom sur le nouveau poste."),
    AppConfig("office-modeles", "Office : modèles (Normal.dotm...)", [("appdata", "Microsoft/Templates")]),
    AppConfig("office-dictionnaires", "Office : dictionnaires personnels", [("appdata", "Microsoft/UProof")]),
    AppConfig("excel-demarrage", "Excel : dossier XLSTART (PERSONAL.XLSB, macros perso)",
              [("appdata", "Microsoft/Excel/XLSTART")]),
    AppConfig("word-demarrage", "Word : dossier STARTUP (macros, compléments)", [("appdata", "Microsoft/Word/STARTUP")]),
    AppConfig("word-blocs", "Word : blocs de construction", [("appdata", "Microsoft/Document Building Blocks")]),
    AppConfig("pense-betes", "Pense-bêtes Windows (Sticky Notes)",
              [("localappdata", "Packages/Microsoft.MicrosoftStickyNotes_8wekyb3d8bbwe/LocalState")],
              note="Se resynchronise aussi avec un compte Microsoft."),
    AppConfig("barre-des-taches", "Raccourcis épinglés à la barre des tâches",
              [("appdata", "Microsoft/Internet Explorer/Quick Launch/User Pinned")],
              note="Les raccourcis ne fonctionnent que si les logiciels sont réinstallés au même endroit."),
    AppConfig("notepadpp", "Notepad++ (réglages, macros, sauvegardes)", [("appdata", "Notepad++")]),
    AppConfig("vscode", "VS Code (réglages, raccourcis, snippets)", [("appdata", "Code/User")],
              extra_excludes=["workspaceStorage", "History", "CachedExtensionVSIXs"],
              note="Extensions : activer « Settings Sync » ou les réinstaller."),
    AppConfig("terminal-windows", "Windows Terminal : réglages",
              [("localappdata", "Packages/Microsoft.WindowsTerminal_8wekyb3d8bbwe/LocalState/settings.json")]),
    AppConfig("polices", "Polices installées pour l'utilisateur", [("localappdata", "Microsoft/Windows/Fonts")]),
    AppConfig("keepass", "KeePass : configuration", [("appdata", "KeePass")],
              note="La base de mots de passe (.kdbx) est un fichier à part : voir « fichiers remarquables »."),
    AppConfig("pcsoft-roaming", "PC SOFT (WinDev/WebDev) : réglages utilisateur", [("appdata", "PC SOFT")],
              note="Réglages de l'éditeur. La licence n'est pas ici : voir les conseils PC SOFT."),
    AppConfig("pcsoft-local", "PC SOFT : données locales", [("localappdata", "PC SOFT")], cache=True),
    AppConfig("pcsoft-programdata", "PC SOFT : données partagées (ProgramData)", [("programdata", "PC SOFT")], default=False,
              note="Peut contenir la configuration serveur HFSQL ou des licences : vérifier avant de cocher."),
    AppConfig("heidisql", "HeidiSQL (sessions, historique)", [("appdata", "HeidiSQL")], registry=[r"HKCU\Software\HeidiSQL"],
              sensitive=True, note="Les sessions (serveurs, mots de passe faiblement chiffrés) sont dans le registre."),
    AppConfig("keepassxc", "KeePassXC : configuration", [("appdata", "KeePassXC")],
              note="La base de mots de passe (.kdbx) est un fichier à part : voir « fichiers remarquables »."),
    AppConfig("obsidian", "Obsidian : réglages et liste des coffres", [("appdata", "obsidian")]),
    AppConfig("tightvnc", "TightVNC (serveur/viewer)", registry=[r"HKCU\Software\TightVNC"], sensitive=True),
    AppConfig("ssh", "Clés et configuration SSH", [("home", ".ssh")], sensitive=True,
              note="Contient des clés privées : n'utiliser qu'un support chiffré."),
    AppConfig("git", "Configuration Git", [("home", ".gitconfig")]),
    AppConfig("aws", "Identifiants AWS", [("home", ".aws")], sensitive=True, note="Clés d'accès : support chiffré uniquement."),
    AppConfig("kube", "Configuration Kubernetes", [("home", ".kube")], sensitive=True),
    AppConfig("filezilla", "FileZilla (sites enregistrés)", [("appdata", "FileZilla")], sensitive=True,
              note="Les mots de passe y sont stockés en clair (encodés) : support chiffré uniquement."),
    AppConfig("winscp", "WinSCP (sessions)", [("appdata", "WinSCP.ini")], registry=[r"HKCU\Software\Martin Prikryl"],
              sensitive=True),
    AppConfig("putty", "PuTTY (sessions, clés d'hôtes)", registry=[r"HKCU\Software\SimonTatham"]),
    AppConfig("odbc", "Sources de données ODBC (utilisateur)", registry=[r"HKCU\Software\ODBC"],
              note="Les pilotes ODBC correspondants doivent être installés sur le nouveau poste."),
]

# Applications qui n'ont pas besoin d'être migrées à la main : composants, pilotes, mises à jour,
# éléments déjà fournis par Windows/Microsoft 365, agents déployés par la DSI.
COMPONENT_RE = re.compile(
    r"redistributable|visual c\+\+|\.net (runtime|framework|core|desktop|sdk)|desktop runtime|runtime package|windowsappruntime|"
    r"windows sdk|software development kit|webview2|update for|hotfix|security update|"
    r"(?<!odbc )(?<!jdbc )\bdriver\b|pilote|vcredist|intel\(r\)|realtek|nvidia (graphics|hd audio|physx)|"
    r"language pack|microsoft update health|windows (app certification|driver package)|"
    r"assistant d.installation de windows|contr.le d.int.grit. du pc|application compatibility|"
    r"intune|management extension|maintenance service|glpi agent|ad lds|"
    r"microsoft (outlook|word|excel|powerpoint|onenote|access|publisher) (20\d\d|365) - |teams meeting add-in",
    re.I,
)

# Applications courantes que winget ne « reconnaît » pas toujours sur un poste (installées par MSI/EXE) mais qu'il sait installer.
# (regex sur le nom, identifiant winget). À relire : ce sont des suggestions, pas une correspondance certaine.
WINGET_GUESS = [
    (r"^google chrome$", "Google.Chrome"),
    (r"^mozilla firefox", "Mozilla.Firefox"),
    (r"^winscp", "WinSCP.WinSCP"),
    (r"^putty", "PuTTY.PuTTY"),
    (r"^vlc media player", "VideoLAN.VLC"),
    (r"^zoom", "Zoom.Zoom"),
    (r"^filezilla", "TimKosse.FileZilla.Client"),
]

# Conseils déclenchés par la présence d'un logiciel : (regex sur le nom, niveau, conseil)
APP_ADVICE = [
    (r"outlook|microsoft (365|office)|office (16|15|professional|standard|home)", "important",
     "Office/Outlook : les comptes Exchange/Microsoft 365 se reconfigurent tout seuls, mais les fichiers .pst (archives) "
     "sont à copier puis à rattacher (Fichier > Ouvrir et exporter > Ouvrir un fichier de données Outlook). "
     "Les .ost sont de simples caches : inutile de les copier."),
    (r"vpn|anyconnect|forticlient|globalprotect|openvpn|pulse secure|zscaler|wireguard", "important",
     "Client VPN détecté : le profil/la configuration ne se copie pas toujours. Demandez à la DSI le profil ou le certificat associé."),
    (r"keepass|bitwarden|1password|lastpass|dashlane", "important",
     "Gestionnaire de mots de passe : vérifiez que la base (ou la synchronisation cloud) est accessible depuis le nouveau poste AVANT d'éteindre l'ancien."),
    (r"sage|ciel|ebp|quickbooks|cegid|divalto", "critique",
     "Logiciel de gestion/compta : ne copiez pas simplement son dossier. Utilisez sa sauvegarde/restauration intégrée et reprenez la licence/le contrat."),
    (r"sql server|mysql|postgres|mariadb|mongodb|oracle database|sqlite|firebird", "critique",
     "Serveur de base de données local : faire un dump/une sauvegarde native (pas une copie de fichiers), puis restaurer sur le nouveau poste."),
    (r"autocad|revit|inventor|solidworks|catia|archicad|sketchup|fusion", "important",
     "Logiciel de CAO : licence (réseau ou compte), bibliothèques, gabarits (.dwt) et profils utilisateur à rapatrier."),
    (r"adobe|photoshop|illustrator|indesign|acrobat pro|premiere", "info",
     "Suite Adobe : se connecter avec le compte pour réactiver la licence ; désactiver l'ancien poste (Compte > Appareils)."),
    (r"docker", "important",
     "Docker : les images et volumes ne sont PAS migrés. Exportez les volumes utiles ou reconstruisez depuis vos Dockerfile/compose."),
    (r"\bwsl\b|ubuntu|debian|kali|windows subsystem for linux", "important",
     "WSL : exportez chaque distribution avec « wsl --export <distro> fichier.tar » puis « wsl --import » sur le nouveau poste."),
    (r"virtualbox|vmware|hyper-v|parallels", "important",
     "Virtualisation : les machines virtuelles sont de gros fichiers (voir « gros fichiers ») à exporter/copier à part."),
    (r"teamviewer|anydesk|rustdesk", "info",
     "Prise en main à distance : l'identifiant change sur un nouveau poste ; prévenir les personnes qui se connectent à vous."),
    (r"veracrypt|truecrypt|bitlocker", "important",
     "Volumes chiffrés : récupérez les mots de passe/fichiers clés/clés de récupération avant la migration."),
    (r"onedrive|dropbox|google drive|nextcloud|owncloud|pcloud|mega", "info",
     "Synchronisation cloud : se reconnecter sur le nouveau poste et laisser la synchronisation se terminer avant de vider l'ancien."),
    (r"teams|slack|zoom|webex|discord|skype", "info",
     "Messagerie/visio : reconnexion avec le compte ; les discussions sont côté serveur, pas besoin de les copier."),
    (r"python|node\.?js|java|jdk|golang|rust|\bgit\b|visual studio|jetbrains|intellij|pycharm|android studio", "info",
     "Outils de développement : réinstaller les versions utilisées (voir liste) ; les dossiers de projets sont copiés, mais pas node_modules/.venv (à régénérer)."),
    (r"pc ?soft|windev|webdev|hfsql", "critique",
     "PC SOFT (WinDev/WebDev/HFSQL) : 1) les licences (numéro + clé, ou dongle) sont à retrouver et à transférer/réactiver avec PC SOFT ou votre revendeur ; "
     "2) réinstallez la MÊME version que celle de vos projets (un projet ouvert dans une version plus récente est converti) ; "
     "3) les données HFSQL (.fic/.ndx/.mmo) ne se copient proprement qu'arrêtées (service ou application fermés) ou via une sauvegarde HFSQL Control Center ; "
     "4) les dossiers de projets et de données sont repérés et proposés à part (« PC SOFT : projet… »), vous pouvez les rediriger avec une règle (fichier regles.txt)."),
    (r"wireguard", "important",
     "WireGuard : les tunnels sont stockés chiffrés par Windows et ne se copient pas. Dans l'application : « Exporter tous les tunnels vers un zip », "
     "puis les importer sur le nouveau poste."),
    (r"packet tracer|networking academy", "info",
     "Cisco Packet Tracer : se reconnecter avec le compte Networking Academy ; vos fichiers .pkt sont dans vos dossiers (copiés)."),
    (r"power ?bi", "important",
     "Power BI Desktop : les .pbix sont copiés, mais les identifiants des sources de données sont à ressaisir (Fichier > Options > Paramètres de source de données)."),
    (r"laragon|xampp|wamp|mamp", "important",
     "Serveur web local (Laragon/XAMPP/WAMP) : copier le dossier des sites (ex. C:\\laragon\\www) mais faire un dump SQL des bases plutôt que copier leurs fichiers."),
    (r"obsidian", "info",
     "Obsidian : un « coffre » est un simple dossier (contenant .obsidian) ; il est copié avec vos dossiers, sauf s'il est hors profil (voir « Autres disques »)."),
    (r"mcafee|norton|kaspersky|crowdstrike|sentinelone|symantec|sophos|defender for endpoint", "info",
     "Antivirus/EDR : géré par la DSI, ne pas le migrer à la main."),
]

# Extensions -> (logiciel probable, regex pour le repérer dans les applis installées, conseil)
EXT_HINTS = {
    ".wdp": ("WinDev (projet)", r"windev|pc ?soft", "Projet WinDev : réinstaller la même version de WinDev."),
    ".wwp": ("WebDev (projet)", r"webdev|pc ?soft", "Projet WebDev : réinstaller la même version de WebDev."),
    ".wpp": ("WinDev Mobile (projet)", r"windev|pc ?soft", "Projet WinDev Mobile : réinstaller la même version."),
    ".fic": ("Données HFSQL", r"hfsql|windev|webdev|pc ?soft", "Fichiers de données HFSQL : à copier applications/service arrêtés."),
    ".ndx": ("Index HFSQL", r"hfsql|windev|webdev|pc ?soft", "Index des fichiers HFSQL (suivent les .fic)."),
    ".mmo": ("Mémos HFSQL", r"hfsql|windev|webdev|pc ?soft", "Mémos des fichiers HFSQL (suivent les .fic)."),
    ".pst": ("Outlook (archives)", r"outlook|office", "Archive de courriers à rattacher dans Outlook sur le nouveau poste."),
    ".kdbx": ("KeePass", r"keepass", "Base de mots de passe : copier ET installer KeePass."),
    ".pfx": ("Certificat + clé privée", r"", "Garder le mot de passe du fichier ; à réimporter sur le nouveau poste."),
    ".p12": ("Certificat + clé privée", r"", "Garder le mot de passe du fichier ; à réimporter sur le nouveau poste."),
    ".ppk": ("Clé PuTTY/WinSCP", r"putty|winscp|filezilla", "Clé privée SSH : confidentiel."),
    ".ovpn": ("OpenVPN", r"openvpn", "Profil VPN."),
    ".rdp": ("Bureau à distance", r"", "Connexions RDP enregistrées."),
    ".dwg": ("AutoCAD / CAO", r"autocad|draftsight|bricscad|libre ?cad", "Vérifier version et licence."),
    ".dxf": ("AutoCAD / CAO", r"autocad|draftsight|bricscad|libre ?cad", "Vérifier version et licence."),
    ".rvt": ("Revit", r"revit", "Vérifier version et licence."),
    ".sldprt": ("SolidWorks", r"solidworks", "Vérifier version et licence."),
    ".sldasm": ("SolidWorks", r"solidworks", "Vérifier version et licence."),
    ".pbix": ("Power BI Desktop", r"power ?bi", "À installer."),
    ".vsdx": ("Visio", r"visio", "À installer (licence Office séparée)."),
    ".mpp": ("MS Project", r"project", "À installer (licence séparée)."),
    ".accdb": ("Access", r"access|office", "Access n'est pas toujours inclus dans la suite Office."),
    ".mdb": ("Access", r"access|office", "Access n'est pas toujours inclus dans la suite Office."),
    ".xlsm": ("Excel (macros)", r"excel|office", "Classeurs avec macros : vérifier les compléments/références."),
    ".xlsb": ("Excel (macros)", r"excel|office", "Classeurs binaires, parfois avec macros."),
    ".one": ("OneNote", r"onenote|office", "Carnets OneNote : souvent déjà sur OneDrive."),
    ".psd": ("Photoshop", r"photoshop|gimp|affinity", "Vérifier licence."),
    ".ai": ("Illustrator", r"illustrator|inkscape|affinity", "Vérifier licence."),
    ".indd": ("InDesign", r"indesign|affinity|scribus", "Vérifier licence."),
    ".prproj": ("Premiere Pro", r"premiere|davinci|vegas", "Vérifier licence."),
    ".drawio": ("draw.io", r"draw\.?io|diagrams", "À installer ou utiliser en ligne."),
    ".sln": ("Visual Studio", r"visual studio", "À installer avec les bonnes charges de travail."),
    ".ipynb": ("Jupyter / Python", r"python|anaconda|jupyter", "Recréer les environnements (requirements)."),
    ".mdf": ("SQL Server", r"sql server", "Base attachée : passer par une sauvegarde native."),
    ".bak": ("Sauvegardes (SQL ?)", r"", "À examiner : peut être une sauvegarde de base de données."),
    ".vmdk": ("Machine virtuelle", r"vmware|virtualbox", "Gros fichier : export/copie à part."),
    ".vhdx": ("Machine virtuelle", r"hyper-v|virtualbox", "Gros fichier : export/copie à part."),
    ".ova": ("Machine virtuelle", r"vmware|virtualbox", "Gros fichier : export/copie à part."),
    ".blend": ("Blender", r"blender", "À installer."),
    ".qbw": ("QuickBooks", r"quickbooks", "Utiliser la sauvegarde intégrée."),
}

GENERIC_ADVICE = [
    ("important",
     "Licences : les clés et comptes des logiciels payants ne se récupèrent pas automatiquement. Pour chaque logiciel payant "
     "de la liste, notez le compte, la clé ou le serveur de licences AVANT l'arrêt de l'ancien poste."),
    ("important",
     "Navigateurs : les mots de passe et cookies sont chiffrés avec votre session Windows et ne se copient pas. "
     "Activez la synchronisation (compte Google/Microsoft/Firefox) sur l'ancien poste et vérifiez qu'elle est terminée."),
    ("info",
     "À reconfigurer à la main : authentificateur (2FA), identifiants Windows enregistrés (voir liste), comptes de messagerie "
     "non Exchange, signatures dans les applications web."),
    ("info",
     "Ne réinitialisez ni ne restituez l'ancien poste avant d'avoir travaillé plusieurs jours sur le nouveau."),
]
