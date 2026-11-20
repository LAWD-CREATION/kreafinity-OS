# Installer Kreafinity

Kreafinity s'installe **pour votre compte**, dans votre dossier personnel. Aucun droit
administrateur n'est nécessaire, sauf pour installer d'éventuels paquets système manquants : le
script vous les liste et attend votre accord avant de lancer `sudo apt-get install`.

Systèmes pris en charge : Debian 12 et plus récents, Ubuntu 22.04 et plus récents et leurs
dérivées (Linux Mint, Pop!_OS, elementary…), avec Python 3.11 ou plus récent (installé si besoin).
Les autres distributions Linux fonctionnent si vous installez vous-même les paquets équivalents.

## En une commande

```bash
curl -fsSL https://raw.githubusercontent.com/LAWD-CREATION/kreafinity-OS/main/packaging/install.sh | bash
```

Lancez cette commande **sans** `sudo`. Le script :

1. installe les paquets système manquants, après votre accord : `python3-venv`, `xdg-utils` et
   `libglib2.0-bin` (ouvrir des applications, la corbeille), `pkexec` (fenêtre de mot de passe
   pour installer des logiciels), plus GTK et WebKitGTK pour la bulle ;
2. crée un environnement Python dédié dans `~/.local/share/kreafinity/venv` et y installe
   Kreafinity ;
3. ajoute les commandes `kreafinity` et `kreafinity-core` dans `~/.local/bin` ;
4. intègre Kreafinity à votre session (`kreafinity setup`) : démarrage automatique du démon, entrée
   « Kreafinity » dans le menu des applications, bulle en bas à droite de l'écran à chaque
   ouverture de session ;
5. vous demande quelle IA utiliser : Ollama (sur l'ordinateur), ou en ligne avec une clé API :
   Ollama Cloud, Claude, GPT, Mistral ou OpenRouter ; ou plus tard ;
6. affiche la bulle.

Sur **KDE Plasma**, la bulle est remplacée par l'intégration au bureau (recherche dans le menu
démarrer, bouton ∞ dans la barre des tâches) ; le script propose aussi le thème Kreafinity et,
si vous le voulez, une disposition à la façon de Windows. Voir [bureau-kde.md](bureau-kde.md).

Ensuite, essayez par exemple : « Montre-moi les 5 plus gros fichiers de mon dossier
Téléchargements et mets-les à la corbeille ». L'IA liste les fichiers, vous montre ce qu'elle
s'apprête à faire et attend votre accord.

### Options

Les options se passent après `bash -s --` :

```bash
curl -fsSL https://raw.githubusercontent.com/LAWD-CREATION/kreafinity-OS/main/packaging/install.sh | bash -s -- --provider=ollama
```

| Option | Effet |
|---|---|
| `--yes`, `-y` | répondre oui à toutes les questions (installation sans surveillance) |
| `--provider=NOM` | IA à configurer : `ollama`, `ollama-cloud`, `anthropic`, `openai`, `mistral`, `openrouter` ou `later` |
| `--no-system-packages` | ne rien installer avec apt (les paquets absents sont seulement signalés) |
| `--no-bubble` | pas de bulle sur le bureau (serveur, session sans écran) |
| `--no-plasma` | ne pas intégrer Kreafinity à KDE Plasma (recherche, bouton, thème) |
| `--allow-root` | autoriser l'installation pour root (conteneurs de test uniquement) |
| `--help` | afficher l'aide |

| Variable | Effet |
|---|---|
| `KREAFINITY_REF` | branche ou étiquette GitHub à installer (`main` par défaut) |
| `KREAFINITY_SOURCE` | dossier ou archive à installer à la place (ex. un clone local) |
| `KREAFINITY_OLLAMA_MODEL` | modèle local téléchargé avec Ollama (`llama3.1` par défaut) |

Exemple pour essayer une branche en cours de développement :

```bash
curl -fsSL https://raw.githubusercontent.com/LAWD-CREATION/kreafinity-OS/ma-branche/packaging/install.sh \
  | KREAFINITY_REF=ma-branche bash
```

Depuis un clone du dépôt : `KREAFINITY_SOURCE=. bash packaging/install.sh`.

## Ce qui est installé, et où

| Élément | Emplacement |
|---|---|
| Programme (environnement Python) | `~/.local/share/kreafinity/venv/` |
| Commandes | `~/.local/bin/kreafinity`, `~/.local/bin/kreafinity-core` |
| Service du démon (systemd utilisateur) | `~/.config/systemd/user/kreafinity.service` |
| Entrée du menu et icône | `~/.local/share/applications/kreafinity.desktop`, `~/.local/share/icons/hicolor/scalable/apps/kreafinity.svg` |
| Bulle à l'ouverture de session | `~/.config/autostart/kreafinity-bubble.desktop` |
| Réglages | `~/.config/kreafinity/config.yaml` |
| Conversations, journal des actions, jeton local | `~/.local/share/kreafinity/` (`history.db`, `actions.log`, `token`) |
| Clés API | trousseau système (GNOME Keyring, KWallet…), jamais dans un fichier |

## Démarrage du démon

Le démon `kreafinity-core` écoute uniquement sur `127.0.0.1:27494`. Il est lancé par un **service
systemd utilisateur** lié à la session graphique : il démarre avec votre bureau (pour pouvoir
ouvrir des applications) et s'arrête quand vous vous déconnectez.

```bash
systemctl --user status kreafinity        # état du démon
journalctl --user -u kreafinity -f        # son journal en direct
systemctl --user restart kreafinity       # le redémarrer (après une mise à jour, par exemple)
```

Sans systemd (conteneur, session SSH, autre système d'initialisation), rien ne change pour vous :
la bulle et `kreafinity ui` lancent le démon à la demande ; son journal est alors dans
`~/.local/share/kreafinity/daemon.log`. Les commandes `kreafinity ask` et `kreafinity chat`
n'ont pas besoin du démon : elles dialoguent directement avec l'IA.

La commande `kreafinity setup` refait cette intégration à tout moment ; `kreafinity setup
--no-bubble` la refait sans la bulle, `kreafinity setup --uninstall` la retire.

## Choisir ou changer d'IA

```bash
kreafinity provider list                  # fournisseurs disponibles, clés, IA par défaut
kreafinity provider use ollama/llama3.1   # un modèle local (installez Ollama : https://ollama.com)
kreafinity provider key anthropic         # enregistrer une clé dans le trousseau
kreafinity provider use anthropic         # Claude par défaut
kreafinity provider key ollama-cloud      # Ollama Cloud (clé créée sur ollama.com)
kreafinity provider use ollama-cloud/gpt-oss:120b
kreafinity provider key openrouter        # OpenRouter (clé créée sur openrouter.ai)
kreafinity provider use openrouter/anthropic/claude-opus-5.5
```

Tout cela se règle aussi dans l'écran **Réglages** de l'interface (`kreafinity ui`).

## Mettre à jour

Kreafinity vérifie une fois par jour si une nouvelle version est publiée sur GitHub. Quand
c'est le cas, une notification apparaît sur le bureau et un bandeau **« Une mise à jour de
Kreafinity est disponible »** s'affiche dans l'interface et dans le panneau de l'assistant, avec
la liste des nouveautés. Un clic sur **Mettre à jour** installe la nouvelle version en
arrière-plan, puis redémarre Kreafinity ; la page se recharge toute seule. Vos réglages, clés API
et conversations sont conservés.

En ligne de commande :

```bash
kreafinity update --check     # y a-t-il une mise à jour ?
kreafinity update             # l'installer
kreafinity version            # version installée (et son commit)
```

La vérification envoie une requête anonyme à `api.github.com` (aucune donnée personnelle). Pour
la désactiver : Réglages › Général › « Rechercher les mises à jour chaque jour », ou
`check_updates: false` dans `~/.config/kreafinity/config.yaml`. Les éléments du bureau KDE (bouton
de la barre des tâches) prennent la nouvelle version à la prochaine ouverture de session.

Une copie de développement (`KREAFINITY_SOURCE`, ou `pip install -e .`) se met à jour avec
`git pull`.

## Désinstaller

```bash
kreafinity uninstall            # retire le programme, le service, le menu et la bulle
kreafinity uninstall --purge    # efface aussi réglages, conversations, journal et clés API
```

Sans `--purge`, vos réglages et conversations restent dans `~/.config/kreafinity` et
`~/.local/share/kreafinity` : une réinstallation les retrouve. Les paquets système installés par
le script (Python, GTK…) sont laissés en place, car d'autres programmes peuvent s'en servir.

## En cas de problème

**`kreafinity : commande introuvable`** — `~/.local/bin` n'est pas encore dans votre `PATH` :
ouvrez un nouveau terminal ou une nouvelle session. En attendant :
`~/.local/bin/kreafinity`.

**La bulle n'apparaît pas** — elle a besoin de GTK 3 et WebKitGTK pour le Python du système :
`sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-webkit2-4.1`, puis `kreafinity setup` et
`kreafinity bubble`. Sous Wayland, la bulle passe par XWayland (présent par défaut sur GNOME et
KDE). Son journal : `~/.local/share/kreafinity/bubble.log`.

**« Aucun trousseau système n'est disponible »** — installez et déverrouillez un trousseau
(`gnome-keyring` sous GNOME, KWallet sous KDE), ou donnez la clé par une variable d'environnement
(`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `MISTRAL_API_KEY`). Pour le démon lancé par systemd :
`systemctl --user set-environment ANTHROPIC_API_KEY=…` puis `systemctl --user restart
kreafinity` (la variable est oubliée à la fin de la session).

**« Impossible de joindre Ollama »** — vérifiez qu'Ollama tourne (`systemctl status ollama`, ou
`ollama serve` dans un terminal) et que le modèle est téléchargé (`ollama list`, sinon
`ollama pull llama3.1`).

**Le port 27494 est déjà utilisé** — choisissez-en un autre dans `~/.config/kreafinity/config.yaml`
(`port: 27500`), puis `systemctl --user restart kreafinity`.

**L'IA ne peut pas ouvrir d'applications** — le démon ne connaît pas votre écran. GNOME et KDE le
lui donnent d'eux-mêmes ; ailleurs, depuis un terminal de votre session graphique :
`systemctl --user import-environment DISPLAY WAYLAND_DISPLAY XAUTHORITY` puis
`systemctl --user restart kreafinity`.

## Pour les développeurs

```bash
git clone https://github.com/LAWD-CREATION/kreafinity-OS && cd kreafinity-OS
python3 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"
pytest
```
