# Étape 3 : la distribution Kreafinity OS

But de l'étape : installer Kreafinity OS comme Windows ou Ubuntu, depuis une clé USB, avec
KDE Plasma, le thème Kreafinity et l'assistant déjà prêts.

| Étape | Contenu | État |
|---|---|---|
| 1 | **Paquet Debian** (`.deb`) de Kreafinity | ✅ fait (ce document) |
| 2 | **Dépôt APT** : `apt install kreafinity` et mises à jour avec le système | ✅ fait, à activer (voir plus bas) |
| 3 | **Image ISO** Debian 13 + KDE Plasma + Kreafinity (live-build) | configurée, premier essai en cours |
| 4 | **Clé USB live** : essayer sans rien installer | avec l'ISO |
| 5 | **Installateur graphique** Calamares aux couleurs Kreafinity | à faire |
| 6 | **Premier démarrage guidé** : choisir son IA, préparer la voix | à faire |

## Le paquet Debian

### Ce qu'il contient

Un paquet **autonome** : Kreafinity et **son propre Python** (3.13) sont dans `/opt/kreafinity`.
Le même paquet marche donc sur Debian 12 et 13 et sur Ubuntu 24.04, quel que soit leur Python,
et rien ne se mélange avec les paquets Python du système ou de l'utilisateur.

Pour **tous les utilisateurs** de l'ordinateur :

| Élément | Fichier |
|---|---|
| Commandes `kreafinity` et `kreafinity-core` | `/usr/bin/` |
| Démon (service systemd utilisateur), activé pour tous | `/usr/lib/systemd/user/kreafinity.service` |
| Entrée du menu des applications et icône | `/usr/share/applications/`, `/usr/share/icons/` |
| Bulle à l'ouverture de session (sauf sur KDE, qui a son widget) | `/etc/xdg/autostart/kreafinity-bubble.desktop` |
| KDE : recherche du menu démarrer | `/usr/share/krunner/dbusplugins/`, `/usr/share/dbus-1/services/` |
| KDE : widget ∞ de la barre des tâches (Plasma 5 ou 6, choisi à l'installation) | `/usr/share/plasma/plasmoids/` |
| KDE : thème global, couleurs, fond d'écran | `/usr/share/plasma/look-and-feel/`, `color-schemes/`, `wallpapers/` |
| KDE : sous-menu Kreafinity du clic droit (Dolphin) | `/usr/share/kio/servicemenus/` |

Les données de chacun (réglages, conversations, mémoire, modèles de voix) restent dans son
dossier personnel, comme avec le script d'installation.

### Construire le paquet

```bash
packaging/debian/build-deb.sh            # → dist/kreafinity_<version>_amd64.deb (≈ 50 Mo)
```

Il faut `uv` (qui télécharge le Python du paquet), `dpkg-deb` et un accès à Internet (PyPI).
La version vient du projet et du commit : `0.1.0~dev0+git20261121.abc1234`. Le script vérifie
que Kreafinity démarre avec le seul contenu du paquet avant de le construire.

La CI de GitHub construit le paquet à chaque modification, l'installe avec APT sur une machine
propre, l'essaie (commandes, démon, interface web), le désinstalle, puis le garde en
téléchargement : **Actions › le dernier passage › Artifacts › kreafinity-deb**.

### Installer, mettre à jour, retirer

```bash
sudo apt install ./kreafinity_0.1.0~dev0+git20261121.abc1234_amd64.deb
```

APT installe aussi ce qui est recommandé : `pkexec` (installation de logiciels), GTK et
WebKitGTK (la bulle), les outils du son. Le démon démarre à la prochaine ouverture de session ;
d'ici là, « Kreafinity » dans le menu le lance aussi. Sur KDE Plasma, chaque utilisateur peut
ajouter le widget à sa barre et le raccourci Meta+C avec `kreafinity desktop install`.

- **Mises à jour** : avec celles du système (`sudo apt upgrade`, ou la logithèque, une fois le
  dépôt APT en place). Le bouton « Mettre à jour » de l'interface l'indique, au lieu de tenter
  de modifier `/opt` lui-même.
- **La voix** : `/opt` étant en lecture seule, ses moteurs s'installent dans le dossier de
  l'utilisateur (`~/.local/share/kreafinity/voice/site-packages`).
- **Retirer** : `sudo apt remove kreafinity` (ou `purge`). Les données de chacun restent ; pour
  les effacer aussi, chaque utilisateur lance d'abord `kreafinity uninstall --purge`.

### Essayé

Sur un **Ubuntu 24.04 neuf** (créé avec `mmdebstrap`) : installation avec APT et ses
dépendances, commandes en tant qu'utilisateur ordinaire, question à l'IA, démon et interface
web, message des mises à jour, voix installable dans le dossier de l'utilisateur, puis
`apt remove` et `apt purge` sans rien laisser.

Pas encore essayé sur un vrai **Debian 13** : ses miroirs n'étaient pas accessibles depuis
l'environnement de développement. C'est la base prévue de l'ISO, le prochain essai à faire.

## Le dépôt APT

Le dépôt est publié sur **GitHub Pages**, à l'adresse
<https://lawd-creation.github.io/kreafinity-OS/> : une page explique l'installation, et APT y
trouve les paquets. La CI le reconstruit et le **signe** à chaque modification de `main`
(`.github/workflows/apt.yml`).

### Installer depuis le dépôt

```bash
curl -fsSLo /tmp/kreafinity.deb https://lawd-creation.github.io/kreafinity-OS/kreafinity_latest_amd64.deb
sudo apt install /tmp/kreafinity.deb
```

Le paquet ajoute lui-même le dépôt (`/etc/apt/sources.list.d/kreafinity.sources`, avec la clé
dans `/usr/share/keyrings/kreafinity-archive-keyring.gpg`) : les mises à jour arrivent ensuite
avec celles du système. Il ne touche pas à un dépôt déjà configuré à la main ; `apt purge` le
retire.

### L'activer (une seule fois)

Le dépôt a besoin d'une **clé de signature** : APT refuse un dépôt non signé, et la signature
garantit que les paquets viennent bien de Kreafinity. La partie privée de cette clé ne doit
jamais être dans le code ni envoyée à quelqu'un. Sur votre ordinateur, depuis le dépôt Git :

1. `packaging/apt/new-signing-key.sh` : crée la clé, écrit la clé **publique** dans
   `packaging/apt/kreafinity-archive-keyring.asc` et la clé **privée** dans
   `~/kreafinity-apt-signing-key.asc`.
2. Validez et poussez la clé publique (`git add packaging/apt/kreafinity-archive-keyring.asc`).
3. Sur GitHub : **Settings › Secrets and variables › Actions › New repository secret**, nom
   `APT_SIGNING_KEY`, valeur : tout le contenu de `~/kreafinity-apt-signing-key.asc`.
4. Sur GitHub : **Settings › Pages › Source : GitHub Actions**.
5. Gardez la clé privée sur une clé USB, puis effacez-la de l'ordinateur
   (`shred -u ~/kreafinity-apt-signing-key.asc`). Si elle était perdue, il faudrait une nouvelle
   clé, et chaque ordinateur devrait réinstaller le paquet pour l'avoir.

Tant que le secret n'existe pas, la CI construit le paquet mais ne publie rien (un avertissement
le rappelle). Les paquets construits avant l'étape 2 n'ont pas la clé : ils ne recevront pas de
mises à jour, il faudra les réinstaller une fois depuis le dépôt.

### Comment c'est fait

- `packaging/apt/build-repo.sh` : place les paquets dans `pool/`, crée les index (`Packages`),
  le fichier `Release` avec leurs sommes de contrôle, le signe (`Release.gpg`, `InRelease`) et
  publie la clé publique, le fichier `kreafinity.sources`, le dernier paquet
  (`kreafinity_latest_amd64.deb`) et la page d'accueil.
- Chaque publication remplace la précédente : le dépôt contient la dernière version.
- Les numéros de version contiennent la date et l'heure du commit
  (`0.1.0~dev0+git20261121.143012.abc1234`) : ils augmentent toujours, et APT propose chaque
  nouvelle version.

### Essayé

Sur un **Ubuntu 24.04 neuf**, avec une clé de test et le dépôt servi localement : installation
exactement comme l'indique la page, dépôt ajouté par le paquet, `apt update` sans
avertissement, puis une version 2 publiée et installée par `apt upgrade`. Et deux attaques
refusées par APT : un index modifié (« taille inattendue ») et un dépôt signé par une autre clé
(« NO_PUBKEY »).

## L'image ISO

Une image **Debian 13 « trixie » + KDE Plasma 6 + Kreafinity**, construite avec `live-build`
(l'outil officiel de Debian pour ses images live) :

- **En français** par défaut (langue, clavier, fuseau horaire), modifiable à l'installation.
- **Le bureau Kreafinity dès le premier démarrage** : thème global Kreafinity comme thème par
  défaut de chaque session (`/etc/xdg/kdeglobals`), donc la disposition à la façon de Windows
  (barre en bas, menu ∞, assistant à côté de l'horloge, Meta+C), les couleurs et le fond
  d'écran.
- **Kreafinity installé** (le paquet Debian), son démon démarrant avec la session.
- **Le quotidien** : Firefox, Dolphin, Kate, Okular, Gwenview, Spectacle, l'archiveur, la
  calculatrice, le réseau, le son, le Bluetooth, l'impression, et les micrologiciels courants
  (Wi-Fi…).
- **Essai sans rien installer** (session live, utilisateur `kreafinity`) et **installation**
  avec Calamares (« Installer Debian » pour l'instant : ses couleurs Kreafinity sont l'étape
  suivante).

### Construire l'ISO

Sur GitHub : **Actions › ISO › Run workflow**. L'image (environ 3 Go) est ensuite dans
**Artifacts › kreafinity-os-iso** pendant 14 jours, avec sa somme de contrôle SHA-256. Elle
est aussi construite pour chaque version publiée (étiquette `v0.2.0`…).

Ou sur un ordinateur sous Debian 13, en tant que root (environ 30 minutes et 15 Go) :

```bash
sudo apt install live-build mount squashfs-tools xorriso
packaging/debian/build-deb.sh dist                    # le paquet Kreafinity
sudo packaging/iso/build-iso.sh dist/kreafinity_*.deb dist
```

### La mettre sur une clé USB

Avec **Balena Etcher** ou **Ventoy** (Windows, macOS, Linux), ou sous Linux :

```bash
sudo dd if=kreafinity-os-<version>-amd64.iso of=/dev/sdX bs=4M status=progress conv=fsync
```

(`/dev/sdX` est la clé USB : vérifiez avec `lsblk`, tout son contenu est effacé.) Démarrez
l'ordinateur sur la clé (touche F12, F2 ou Échap selon les modèles) : Kreafinity OS s'ouvre
directement sur le bureau, sans rien installer. L'icône « Installer » lance l'installation.

### Comment c'est fait

- `packaging/iso/build-iso.sh` : configure `live-build` (Debian 13, dépôts main, contrib,
  non-free et non-free-firmware, paramètres de démarrage en français), y ajoute notre
  configuration et le paquet Kreafinity, puis construit l'ISO hybride (BIOS et UEFI, CD ou
  clé USB).
- `packaging/iso/config/` : la liste des paquets, les réglages par défaut du bureau
  (`includes.chroot/etc/xdg/`) et un script qui vérifie l'image (Kreafinity présent, démon
  activé) et met le français par défaut.

## La suite

1. **Calamares** : l'installateur graphique, avec les couleurs et le logo Kreafinity, en
   français par défaut.
2. **Premier démarrage** : un assistant d'accueil (choix de l'IA, clé API, préparation de la
   voix, découverte des fonctions).
