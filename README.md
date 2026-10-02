# swap — assistant de migration de poste

Outil en Python (aucune dépendance, juste Python 3.9+) pour renouveler un poste de travail Windows compliqué :

1. **Il te dit tout ce qu'il faut migrer** : dossiers (y compris ceux posés à la racine de `C:\` ou `D:\`), configurations d'applis, applications installées, imprimantes, lecteurs réseau, certificats, tâches planifiées, ODBC, variables d'environnement…
2. **Il fait la copie à ta place** vers un disque externe ou un partage réseau, avec reprise en cas d'interruption et vérification.
3. **Il remet tout en place** sur le nouveau poste et génère un script qui réinstalle les applis (via `winget`), recrée les lecteurs réseau et les imprimantes.

> Pourquoi Python et pas PHP ? Il faut lire le registre Windows, la liste des logiciels, les imprimantes… PHP n'est pas fait pour ça, Python si.

## Utilisation rapide : l'interface web

Double-cliquer sur `LANCER.bat` (ou `python -m swap web`). swap démarre un petit serveur **sur ce poste uniquement** (127.0.0.1, protégé par un jeton aléatoire) et ouvre l'interface dans une fenêtre Edge/Chrome. Rien à installer d'autre que Python 3.9+ ; aucune connexion Internet n'est nécessaire (tout est dans le dossier). Thème clair ou sombre selon Windows.

Pour arrêter : bouton **Quitter**, ou simplement fermer la fenêtre (swap s'arrête de lui-même après 5 minutes sans page ouverte, sauf si un transfert est en cours). L'ancienne fenêtre tkinter reste disponible : `python -m swap gui`.

En haut : **PC source** (ce poste, détecté) → **PC cible** (à saisir : `TPSEL045` ou une adresse IP).

1. **Analyser** : lit ce poste et prépare la liste de tout ce qui est à migrer (rapport détaillé consultable). Une liste **« Utilisateur à migrer »** propose tous les profils de `C:\Users` (le compte connecté d'abord) : on peut donc migrer le profil d'un collègue depuis un compte administrateur. Voir « Choisir l'utilisateur » plus bas.
2. **Choisir** : cocher / décocher les éléments, filtrer, écrire des règles de destination (« les projets PC SOFT arrivent dans `C:\Mes Projets` »), exclure des fichiers (`*.iso`).
3. **Envoyer**, au choix :
   - **Directement sur le PC cible (recommandé, tout depuis ce poste)** : on saisit le nom du PC cible en haut, on clique sur *Charger* pour lister ses utilisateurs, on choisit celui qui doit recevoir les données et on envoie. Les fichiers sont déposés tels quels dans son profil (`C:\Users\<nom>\Documents`, `AppData`…) par le partage d'administration `\\PC\C$` : **rien à lancer ni à installer sur le PC cible**. Il faut être administrateur sur ce PC, et l'utilisateur doit s'y être connecté au moins une fois. Les fichiers déjà présents sont conservés (case pour les écraser). Un dossier `C:\SWAP\SWAP-<poste>` y reçoit le rapport et `installer_et_configurer.ps1`. Limites : le registre (PuTTY, WinSCP…) et l'installation des applications ne peuvent pas être faits à distance ; ce script est à lancer une fois sur le PC cible. Ligne de commande : `python -m swap copy --host TPSEL045 --to-user jdupont`.
   - **Connexion directe** vers le PC cible : sur ce PC cible, ouvrir swap → onglet 4 → *Démarrer la réception* : il affiche un **code**. Le saisir sur le PC source. Envoi chiffré, avec progression, vitesse, temps restant, **annulation et reprise**.
   - **Partage Windows** `\\TPSEL045\C$\SWAP` : rien à lancer sur le PC cible, mais il faut être administrateur dessus.
   - **Disque externe / dossier** : comme avant.
4. **Recevoir / restaurer (sur le PC cible)** : la liste **« Restaurer dans le profil »** choisit le compte de destination (par défaut celui de la sauvegarde s'il existe sur ce PC). *Restaurer* remet tout en place (sans écraser), puis *Lancer le script d'installation* réinstalle les applications et recrée lecteurs réseau et imprimantes. Le dossier reçu contient aussi `RESTAURER.bat`, qui ouvre directement cet écran.

Le PC cible a besoin de l'outil (le même dossier `swap`, à copier une fois) et de Python pour recevoir en connexion directe. Avec le mode « partage Windows », c'est `RESTAURER.bat` (qui embarque l'outil) qui suffit, mais Python reste nécessaire pour l'exécuter.

### Connexion directe : sécurité et réseau

- Le code affiché (10 caractères) authentifie les deux PC et sert à chiffrer la connexion : sans lui, personne ne peut envoyer ni lire de données. Les échanges sont chiffrés et signés (SHAKE-256 + HMAC-SHA256, bibliothèque standard uniquement). Cette construction **n'a pas été auditée** : elle convient pour un réseau interne, pas pour Internet.
- Le port TCP **47800** doit être autorisé en entrée sur le PC cible (profil Domaine). La case « Ouvrir ce port dans le pare-feu » le fait pour vous si vous avez les droits administrateur, et le referme ensuite. Si votre entreprise filtre les flux entre postes, utilisez le partage Windows ou un disque.
- Équivalent en ligne de commande : `python -m swap receive --dest C:\SWAP --firewall` (cible), puis `python -m swap copy --host TPSEL045 --code XXXXX-XXXXX` (source).

### Choisir l'utilisateur

- **Analyse** : `python -m swap profiles` liste les profils ; `python -m swap scan --user jdupont` analyse celui de `jdupont`. Les dossiers (Bureau, Documents… y compris ceux redirigés vers OneDrive) et la configuration des applications du profil sont lus. Il faut être **administrateur** pour lire le profil d'un autre compte.
- **Limite** : ce qui vit dans la *session* de l'utilisateur n'est lisible que connecté avec son compte : clés de registre (PuTTY, WinSCP, ODBC…), lecteurs réseau, variables d'environnement, certificats personnels, identifiants Windows, applications installées « pour l'utilisateur ». Le rapport le signale quand on analyse un autre compte ; pour tout avoir, lancer l'analyse connecté avec ce compte.
- **Restauration** : `python -m swap restore --backup … --user jdupont` remet tout dans le profil de `jdupont` (Bureau, Documents, AppData…), même si le nom du compte est différent de celui d'origine. Les réglages du registre ne sont importés que pour le compte connecté : ils sont signalés, à relancer connecté avec le compte de destination.
- Le menu texte (`python -m swap` sans interface graphique) propose aussi de choisir le profil.

## Utilisation rapide : ligne de commande

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

### Installateurs des applications non installables par le script

Pour chaque application que `winget` ne sait pas réinstaller, l'analyse cherche sur le poste (Téléchargements, `C:\Temp`, dossiers à la racine…) un fichier d'installation dont le **nom** correspond (`.exe`, `.msi`, `.msix`, `.zip`…). Ceux trouvés deviennent des éléments à part (« Installateurs des applications à installer à la main »), copiés dans la sauvegarde puis remis dans `Téléchargements\Installateurs` sur le nouveau poste, et **lancés par `installer_et_configurer.ps1`** (`msiexec` pour les `.msi`, assistant pour les `.exe`). Le rapport donne un tableau application → installateur, indique les applications **sans** installateur, les autres installateurs trouvés, et signale un lecteur réseau qui ressemble à un dépôt de logiciels (`\\serveur\Install`).

Limite : Windows ne conserve pas l'installateur d'un logiciel une fois installé. S'il n'est plus sur le disque, il faut le retélécharger chez l'éditeur ou le demander à la DSI ; l'outil te le dit.

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
# avec l'ancienne interface tkinter (Linux sans écran) : xvfb-run -a python3 -m unittest discover -s tests
```

Le code d'inventaire est principalement pensé pour Windows ; sous Linux/macOS il analyse les dossiers et la copie/restauration fonctionne, mais pas les applications ni la configuration système.

| Module | Rôle |
|---|---|
| `swap/scan.py` | construit l'inventaire |
| `swap/catalog.py` | connaissances : configs d'applis à emporter, conseils par logiciel, types de fichiers |
| `swap/apps.py`, `swap/system.py` | applis installées, winget, imprimantes, lecteurs, certificats… |
| `swap/transfer.py` | copie, vérification, restauration |
| `swap/report.py`, `swap/scripts.py` | rapport HTML/MD et script PowerShell |
| `swap/sink.py`, `swap/net.py` | destinations de copie : dossier/partage, ou PC distant (TCP chiffré) |
| `swap/session.py`, `swap/gui.py` | logique et fenêtres de l'interface graphique |
