# Ce que l'IA peut faire sur l'ordinateur

Chaque outil a un **niveau de risque**. Les outils `confirm` et `danger` ne s'exécutent
qu'après votre accord, quel que soit le fournisseur d'IA. Pour un outil `danger`, la carte de
validation montre l'action exacte (commande, paquets…) et demande une confirmation explicite.
Chaque outil peut être désactivé dans les **Réglages** (ou avec la clé `tools` de
`~/.config/kreafinity/config.yaml`). Toutes les actions exécutées sont inscrites dans
`~/.local/share/kreafinity/actions.log`.

| Outil | Risque | Rôle |
|---|---|---|
| `system_info` | safe | Système, processeur, mémoire, disques, batterie, volume du son, luminosité |
| `list_directory` | safe | Lister un dossier |
| `search_files` | safe | Chercher des fichiers (par nom, taille, date) |
| `read_file` | safe | Lire un fichier texte |
| `list_applications` | safe | Lister les applications installées |
| `list_windows` | safe | Lister les fenêtres ouvertes (KDE Plasma) |
| `search_conversations` | safe | Chercher dans les conversations passées |
| `memory` | safe | Retenir ou corriger une note de sa mémoire (voir [memoire.md](memoire.md)) |
| `skill` | safe | Lire, enregistrer ou améliorer un savoir-faire |
| `soul` | safe | Faire grandir sa propre personnalité, son âme (voir [memoire.md](memoire.md#son-âme-sa-personnalité)) |
| `create_folder` | confirm | Créer un dossier |
| `create_file` | confirm | Créer un fichier texte (sans jamais écraser) |
| `move_file` | confirm | Déplacer ou renommer |
| `open_file` | confirm | Ouvrir un fichier avec son application |
| `open_url` | confirm | Ouvrir une adresse dans le navigateur |
| `open_application` | confirm | Lancer une application |
| `close_application` | confirm | Demander à une application de se fermer (jamais de force) |
| `set_volume` | confirm | Régler le volume, couper ou rétablir le son |
| `set_brightness` | confirm | Régler la luminosité de l'écran (ordinateurs portables) |
| `arrange_window` | confirm | Mettre une fenêtre au premier plan, la réduire, l'agrandir, la placer à gauche ou à droite (KDE Plasma) |
| `set_appearance` | confirm | Thème clair, sombre ou Kreafinity, et fond d'écran (KDE Plasma, GNOME) |
| `move_to_trash` | danger | Mettre à la corbeille (jamais de suppression définitive) |
| `run_command` | danger | Exécuter une commande avec vos droits |
| `install_package` | danger | Installer des logiciels du système (mot de passe administrateur) |

Les fichiers accessibles sont limités à votre dossier personnel (liste modifiable dans les
réglages). Les dossiers sensibles (`~/.ssh`, trousseaux, données de Kreafinity…) restent
toujours inaccessibles.

## Volume du son

Kreafinity utilise la commande du serveur de son : `wpctl` pour PipeWire (Debian 12,
Ubuntu 22.10 et plus récents), sinon `pactl` pour PulseAudio. Il n'existe pas d'interface
D-Bus standard pour le volume. Le volume est limité à 100 % (pas d'amplification).

## Luminosité de l'écran

La luminosité est changée par **systemd-logind**, via D-Bus (bibliothèque `dbus-fast`) : sans
mot de passe, pour l'utilisateur de la session graphique. Elle ne descend jamais sous 5 %, pour
ne pas laisser un écran noir. Seuls les écrans intégrés (rétroéclairage dans
`/sys/class/backlight`) sont pris en charge, pas les écrans externes.

> Le brief prévoyait `dbus-next` ; il n'est plus maintenu depuis 2021 et laisse ses connexions
> ouvertes. `dbus-fast` est sa suite maintenue, avec la même interface.

## Apparence du bureau (`set_appearance`)

« Passe en mode sombre », « mets cette photo en fond d'écran », « remets les couleurs
Kreafinity » : l'outil change le thème (`dark`, `light` ou `kreafinity`, sombre et violet) et/ou
le fond d'écran (une image de vos dossiers autorisés, ou `kreafinity` pour le fond Kreafinity).
La disposition du bureau (barres, widgets) n'est jamais modifiée.

![Thème Kreafinity puis thème clair, appliqués par l'assistant](img/plasma-apparence.jpg)

- **KDE Plasma** : jeu de couleurs des applications (`plasma-apply-colorscheme` : BreezeDark,
  BreezeLight ou Kreafinity), thème des barres et menus (`plasma-apply-desktoptheme`) et
  `plasma-apply-wallpaperimage`. Le thème Kreafinity demande `kreafinity desktop install`.
- **GNOME** (et le bureau d'Ubuntu) : `gsettings`, schéma de couleurs préféré de l'interface et
  image de fond (`picture-uri`, et `picture-uri-dark` depuis GNOME 42).
- Les autres bureaux ne sont pas pris en charge : l'assistant le dit.

## Fenêtres (`list_windows`, `arrange_window`)

« Mets Firefox à gauche et mes documents à droite », « réduis le terminal », « remets Dolphin
devant » : comme l'ancrage des fenêtres de Windows, l'assistant peut ranger les fenêtres
ouvertes. Une action peut mettre une fenêtre au premier plan (`focus`), la réduire, la rouvrir
(`restore`), l'agrandir (`maximize`) ou la placer sur la moitié gauche ou droite de l'écran.
Pour mettre deux fenêtres côte à côte, il fait deux actions.

![Deux fenêtres placées côte à côte par l'assistant](img/plasma-fenetres.jpg)

Sur KDE Plasma uniquement : Kreafinity envoie à KWin (le gestionnaire de fenêtres de Plasma) un
petit script par D-Bus (`org.kde.kwin.Scripting`), qui lui renvoie sa réponse puis est
déchargé. Fermer une application passe toujours par `close_application`, qui lui demande
poliment de se fermer.

## Commandes (`run_command`)

La commande s'exécute avec `bash`, **avec vos droits** : elle n'est pas limitée aux dossiers
autorisés. C'est pourquoi elle est classée `danger` et affichée en entier avant exécution. Elle
s'exécute sans terminal, sans entrée clavier, dans un dossier autorisé, et elle est arrêtée au
bout de 60 secondes (10 minutes au plus). Les clés API des fournisseurs d'IA ne lui sont pas
transmises, et seuls le début et la fin d'une sortie très longue sont gardés.

Garde-fous, en plus de votre validation : Kreafinity refuse les commandes qui appellent
`sudo`, `su`, `pkexec`… (les droits administrateur passent par `install_package`) et celles qui
suppriment avec `rm`, `shred`… (la suppression passe par la corbeille). Cette détection lit la
ligne de commande ; un programme qui supprimerait des fichiers par un autre moyen ne serait pas
repéré : **lisez la commande avant de la confirmer**.

## Installation de logiciels (`install_package`)

Pour les systèmes APT (Debian, Ubuntu et dérivées). Avant de vous demander votre accord,
Kreafinity simule l'installation (`apt-get -s`, sans droits) : la carte de validation indique
les versions, les paquets supplémentaires nécessaires et la commande exacte. Ensuite :

```
pkexec /usr/bin/apt-get install -y -q -o DPkg::Lock::Timeout=120 -- vlc
```

`pkexec` fait afficher par le système (polkit) sa propre fenêtre de mot de passe
administrateur : Kreafinity ne voit jamais ce mot de passe et n'utilise jamais `sudo`. Il faut
un agent polkit dans la session : c'est le cas de GNOME, KDE et des autres bureaux courants.
Les noms de paquets sont vérifiés (aucun nom ne peut passer pour une option d'`apt-get`).
