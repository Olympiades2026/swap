# swap — assistant de migration de poste

Outil en Python (aucune dépendance, juste Python 3.9+) pour renouveler un poste de travail Windows compliqué :

1. **Il te dit tout ce qu'il faut migrer** : dossiers (y compris ceux posés à la racine de `C:\` ou `D:\`), configurations d'applis, applications installées, imprimantes, lecteurs réseau, certificats, tâches planifiées, ODBC, variables d'environnement…
2. **Il fait la copie à ta place** vers un disque externe ou un partage réseau, avec reprise en cas d'interruption et vérification.
3. **Il remet tout en place** sur le nouveau poste et génère un script qui réinstalle les applis (via `winget`), recrée les lecteurs réseau et les imprimantes.

> Pourquoi Python et pas PHP ? Il faut lire le registre Windows, la liste des logiciels, les imprimantes… PHP n'est pas fait pour ça, Python si.

## Utilisation rapide

Sur l'**ancien** poste (Python installé) :

```
LANCER.bat
```

ou en ligne de commande :

```
python -m swap scan                                   # 1. analyse -> swap-sortie\rapport.html
python -m swap copy --dest E:\Migration -i            # 2. copie (choix interactif des éléments)
python -m swap verify --backup E:\Migration\SWAP-MON-PC   # 3. vérification
```

Sur le **nouveau** poste : brancher le disque, lancer `RESTAURER.bat` (déposé dans le dossier de sauvegarde avec une copie de l'outil), puis relire et exécuter `installer_et_configurer.ps1` dans PowerShell.

## Ce que fait chaque commande

### `scan`
Produit dans `swap-sortie\` :
- `rapport.html` / `rapport.md` : le rapport complet. Il commence par les **points d'attention classés par gravité** (certificats avec clé privée, bases de données locales, logiciels de compta, VPN, licences…), puis la liste de tout ce qui est copiable avec les tailles, les applis à réinstaller (celles réinstallables par winget vs. à la main), les types de fichiers rencontrés et les logiciels qu'ils impliquent (`.dwg` → AutoCAD, `.pbix` → Power BI…), les fichiers « faciles à oublier » (`.pst`, `.kdbx`, `.pfx`, `.rdp`…), les gros fichiers.
- `inventaire.json` : les mêmes données, exploitables par les autres commandes.
- `installer_et_configurer.ps1` : script à lancer sur le nouveau poste.

### `copy`
Copie les éléments cochés par défaut (ou choisis avec `-i`, `--only`, `--skip`) vers `--dest`.
- Une sauvegarde = un dossier `SWAP-<nom du poste>` ; relancer la commande ne recopie **que ce qui a changé**.
- Écriture via fichier temporaire : une interruption ne laisse jamais de fichier tronqué.
- Fichiers verrouillés (Outlook ouvert…) : notés dans `erreurs.log`, la copie continue ; fermer l'appli et relancer.
- `--hash` calcule une empreinte SHA-256 par fichier (plus lent, permet `verify --deep`).
- `--dry-run` simule sans rien écrire. Vérifie aussi la place disponible.
- `--exclude *.iso *.msu` : ne copie pas ces fichiers (images disque, mises à jour… qui se re-téléchargent).
- À la racine des disques, les dossiers qui sont des **logiciels installés** (reconnus par leur nom ou leur éditeur), des installations WinDev ou du temporaire sont **décochés**, mais ce qui vous appartient dedans est proposé à part : `www` de Laragon/XAMPP/WAMP, dossier `Personal` de WinDev/WebDev.
- Les liens/jonctions Windows (« Menu Démarrer », « Voisinage réseau »…) et les dossiers vides sont ignorés.

Ignorés volontairement : corbeille, caches des navigateurs, `node_modules`, `__pycache__`, `.venv`, fichiers temporaires et verrous Office (`~$*`).
Les fichiers OneDrive « en ligne uniquement » ne sont **pas** téléchargés. Les dossiers Bureau/Documents redirigés vers OneDrive sont décochés par défaut (ils se resynchronisent sur le nouveau poste), tu peux les recocher.

### Règles de destination (« les trucs PC SOFT arrivent dans `C:\Mes Projets` »)

Par défaut chaque élément retourne à sa place d'origine (adaptée au nouveau profil). Pour l'envoyer ailleurs, écris une règle dans `swap-sortie\regles.txt` (créé par `scan`) :

```
# motif = dossier de destination
pcsoft-projet  = C:\Mes Projets
pcsoft-donnees = D:\Donnees HFSQL
```

ou en ligne de commande : `--map "pcsoft-projet=C:\Mes Projets"` (sur `copy` ou `restore`).
- Le motif est cherché dans l'identifiant, le nom et le chemin d'origine de l'élément (les identifiants sont dans le rapport). `a|b` = « a ou b ».
- Si une règle vise **plusieurs** éléments (ex. 3 projets), chacun est rangé dans un sous-dossier à son nom : `C:\Mes Projets\GestionStock`, `C:\Mes Projets\Site`…
- La première règle qui correspond l'emporte. Les règles données à `copy` sont enregistrées dans la sauvegarde (donc appliquées par `RESTAURER.bat`) ; celles données à `restore` priment.
- Utilise `restore --dry-run` : il affiche la destination de chaque élément sans rien écrire.

### PC SOFT (WinDev / WebDev / HFSQL)

L'analyse ignore volontairement le dossier d'**installation** de WinDev/WebDev (`C:\PC SOFT\WINDEV…` : exemples, framework, aide), qui se réinstalle, et n'en garde que le dossier `Personal`. Elle repère les projets (`.wdp`, `.wwp`, `.wpp`) et les données HFSQL (`.fic`, `.ndx`, `.mmo`) **où qu'ils soient** (Documents, `C:\Mes Projets`, un autre disque…). Chaque dossier de projet ou de données devient un élément à part (`pcsoft-projet-…`, `pcsoft-donnees-…`), donc redirigeable par une règle, et n'est pas copié en double avec son dossier parent. Le rapport rappelle les pièges : licences/dongle, même version de WinDev pour rouvrir les projets, copie des données HFSQL applications arrêtées.

### `verify`
Compare la sauvegarde à son manifeste (fichiers manquants, tailles, et contenu avec `--deep`).

### `restore`
Remet chaque élément à sa place **sur le nouveau poste**, même si le nom d'utilisateur ou l'emplacement des dossiers (OneDrive…) est différent. **N'écrase jamais** un fichier existant sauf avec `--overwrite`. Réimporte aussi les clés de registre exportées (PuTTY, WinSCP, ODBC).

## Ce que l'outil ne peut PAS faire (et te le dit)

- Récupérer les **mots de passe des navigateurs** (chiffrés par la session Windows) → activer la synchronisation du navigateur.
- Récupérer les **licences** des logiciels payants → à noter avant.
- Exporter les **certificats** du magasin Windows → export `.pfx` manuel ou redemande à la DSI.
- Migrer proprement des **bases de données locales, logiciels de compta, Docker, WSL, VM** : copier leurs fichiers ne suffit pas, le rapport t'indique la bonne méthode quand il les détecte.

## Sécurité

- La sauvegarde peut contenir des données sensibles (marquées 🔒 : clés SSH, sessions FileZilla…). Utilise un disque chiffré (BitLocker To Go) et efface-le après.
- L'outil **ne lit jamais** les mots de passe : il liste seulement les *noms* des identifiants Windows enregistrés.
- Rien n'est supprimé ni modifié sur l'ancien poste : c'est de la lecture seule.

## Développement

```
python -m unittest discover -s tests
```

Le code d'inventaire est principalement pensé pour Windows ; sous Linux/macOS il analyse les dossiers et la copie/restauration fonctionne, mais pas les applications ni la configuration système.

| Module | Rôle |
|---|---|
| `swap/scan.py` | construit l'inventaire |
| `swap/catalog.py` | connaissances : configs d'applis à emporter, conseils par logiciel, types de fichiers |
| `swap/apps.py`, `swap/system.py` | applis installées, winget, imprimantes, lecteurs, certificats… |
| `swap/transfer.py` | copie, vérification, restauration |
| `swap/report.py`, `swap/scripts.py` | rapport HTML/MD et script PowerShell |
