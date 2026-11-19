# Kreafinity OS — Brief projet pour Claude Code

> Système d'exploitation open source piloté par IA, où l'utilisateur choisit librement son fournisseur LLM.
> Édité par **Kreafinity** (agence web & IA, La Réunion) — https://kreafinity.com

---

## 1. Vision

Kreafinity OS est une distribution Linux dont l'IA est le cœur de l'expérience utilisateur :
- L'utilisateur parle à son ordinateur (texte, puis voix) pour lancer des applis, gérer ses fichiers, régler le système.
- **Liberté totale du fournisseur IA** : Claude (Anthropic), GPT (OpenAI), Mistral, ou un modèle 100 % local via Ollama. On change de fournisseur en un clic, sans perdre ses données.
- Interface de bureau familière, inspirée de Windows (barre des tâches, menu démarrer, fenêtres), avec l'IA intégrée partout.
- **Open source** sur GitHub, respect de la vie privée, l'utilisateur garde le contrôle (toute action sensible demande sa validation).

On ne réécrit **pas** de noyau ni de pilotes : on s'appuie sur Linux (base Debian) et on construit les couches supérieures.

## 2. Feuille de route

| Étape | Objectif | Livrable |
|---|---|---|
| **1 (ACTUELLE)** | Assistant système multi-LLM | Démon `kreafinity-core` + CLI + interface web de chat, installable sur n'importe quel Linux |
| 2 | Bureau façon Windows | Shell de bureau avec IA intégrée (barre de recherche IA, panneau assistant) |
| 3 | Distribution | ISO bootable basée sur Debian, installateur, clé USB live |

**Ne travailler que sur l'étape 1** tant qu'elle n'est pas terminée et testée. Garder l'architecture prête pour les étapes 2 et 3 (le cœur doit être indépendant de l'interface).

## 3. Étape 1 — Périmètre fonctionnel

### 3.1 Couche fournisseurs LLM (« providers »)
- Interface commune `LLMProvider` : `chat(messages, tools) -> réponse (streaming)`, `list_models()`, `supports_tools`.
- Adaptateurs à implémenter : **Anthropic**, **OpenAI**, **Mistral**, **Ollama** (local).
- Le format interne des messages et des appels d'outils est **neutre** ; chaque adaptateur convertit vers/depuis le format de son API.
- Changement de fournisseur/modèle à chaud, y compris en pleine conversation.
- Fournisseur par défaut configurable ; repli possible (ex. si pas de connexion Internet → Ollama).

### 3.2 Moteur d'agent
- Boucle agent classique : message utilisateur → LLM → appels d'outils → résultats → LLM → réponse.
- Limite du nombre d'itérations par requête (configurable, défaut 10).
- Historique des conversations stocké localement (SQLite).
- Prompt système en français par défaut, langue configurable.

### 3.3 Outils (ce que l'IA peut faire sur la machine)
Chaque outil déclare un **niveau de risque** :

| Niveau | Comportement | Exemples |
|---|---|---|
| `safe` | Exécuté directement | infos système (CPU, RAM, disque, batterie), lister un dossier, lire un fichier texte, rechercher des fichiers, lister les applis installées |
| `confirm` | Demande validation à l'utilisateur avant exécution | ouvrir/fermer une application, créer/déplacer/renommer un fichier, changer le volume ou la luminosité, ouvrir une URL |
| `danger` | Validation explicite + affichage clair de la commande exacte | supprimer des fichiers, exécuter une commande shell, installer un paquet |

Règles impératives :
- **Aucune** action `confirm`/`danger` sans validation de l'utilisateur, quel que soit le fournisseur.
- Suppression = envoi à la corbeille (spécification freedesktop Trash), jamais `rm` direct.
- Jamais de `sudo` automatique ; les actions root passent par `pkexec`/polkit, donc par une demande système visible.
- Accès fichiers limité au dossier personnel de l'utilisateur par défaut (liste blanche configurable).
- Journal de toutes les actions exécutées (`~/.local/share/kreafinity/actions.log`).

### 3.4 API locale du démon
- Serveur local (127.0.0.1 uniquement) : REST + WebSocket pour le streaming.
- Endpoints : conversations, envoi de message, validation/refus d'une action en attente, liste et choix des fournisseurs/modèles, configuration.
- Authentification par jeton local (fichier lisible seulement par l'utilisateur) pour éviter qu'un site web ou un autre utilisateur pilote le démon.

### 3.5 Interfaces de l'étape 1
- **CLI** : `kreafinity chat`, `kreafinity ask "..."`, `kreafinity provider use ollama/llama3.1`, `kreafinity config`.
- **Interface web de chat** (servie par le démon) : conversation en streaming, sélecteur de fournisseur/modèle, cartes de validation pour les actions `confirm`/`danger`, page de réglages (clés API, fournisseur par défaut, outils autorisés). Design sobre et moderne, aux couleurs Kreafinity, mode sombre.

### 3.6 Configuration et secrets
- Config : `~/.config/kreafinity/config.yaml`.
- **Clés API jamais en clair dans la config ni dans Git** : stockées via le trousseau système (bibliothèque `keyring` / Secret Service).
- `.gitignore` strict ; aucun secret dans les tests ou exemples.

## 4. Stack technique

- **Langage du cœur** : Python 3.11+
- **Serveur** : FastAPI + Uvicorn (REST + WebSocket)
- **Clients LLM** : SDK officiels (`anthropic`, `openai`, `mistralai`) et API HTTP d'Ollama via `httpx`
- **Stockage** : SQLite (via `sqlite3` ou SQLModel)
- **CLI** : Typer + Rich
- **Interface web** : HTML/CSS/JS léger (ou Svelte/React si nécessaire), servie en statique par FastAPI
- **Intégration système** : `psutil`, `gio`/`xdg-open`, D-Bus (`dbus-next`) pour volume/luminosité/notifications
- **Service** : unité `systemd --user` pour lancer le démon au démarrage
- **Tests** : pytest (fournisseurs LLM simulés par des mocks ; aucun appel réseau réel dans les tests)
- **Qualité** : ruff (lint + format), mypy, pre-commit
- **CI** : GitHub Actions (lint + tests)

## 5. Structure du dépôt (proposée)

```
kreafinity-os/
├── CLAUDE.md
├── README.md               # FR + EN, captures/démo, installation
├── LICENSE
├── CONTRIBUTING.md
├── pyproject.toml
├── core/
│   └── kreafinity/
│       ├── providers/      # base.py, anthropic.py, openai.py, mistral.py, ollama.py
│       ├── agent/          # boucle agent, prompts, historique
│       ├── tools/          # base.py (niveaux de risque), system.py, files.py, apps.py, shell.py
│       ├── api/            # serveur FastAPI, routes, websocket, auth
│       ├── config/         # chargement config + keyring
│       └── cli.py
├── ui/web/                 # interface de chat
├── packaging/              # unité systemd, script d'installation
├── tests/
└── docs/                   # architecture, guide d'ajout d'un fournisseur, guide d'ajout d'un outil
```

## 6. Conventions

- Code, noms de variables et commits **en anglais** ; textes de l'interface **en français** (i18n prévue dès le départ, fichiers de traduction).
- Commits courts et clairs (style Conventional Commits : `feat:`, `fix:`, `docs:`…).
- Chaque fournisseur et chaque outil = un fichier, ajoutable sans toucher au reste (registre/plugins).
- Typage complet, docstrings sur les interfaces publiques.
- Avancer par petites étapes testables ; faire tourner les tests avant chaque commit.

## 7. Plan de travail suggéré pour l'étape 1

1. Initialiser le dépôt : `pyproject.toml`, ruff, pytest, pre-commit, CI GitHub Actions, README minimal, licence.
2. Interface `LLMProvider` + adaptateur **Ollama** (gratuit, local, idéal pour tester) + adaptateur **Anthropic**.
3. Boucle agent + 3 outils `safe` (infos système, lister un dossier, rechercher des fichiers) + CLI `kreafinity ask`.
4. Système de validation des actions `confirm`/`danger` + outils fichiers et applications.
5. Adaptateurs **OpenAI** et **Mistral** ; changement de fournisseur à chaud.
6. Démon FastAPI + WebSocket + jeton d'authentification + historique SQLite.
7. Interface web de chat + écran de réglages + stockage des clés dans le trousseau.
8. Unité systemd + script d'installation (`install.sh`) + documentation.

**Critère de fin d'étape 1** : sur un Ubuntu/Debian neuf, on installe en une commande, on choisit Ollama ou Claude, et on peut demander en français « Montre-moi les 5 plus gros fichiers de mon dossier Téléchargements et mets-les à la corbeille » : l'IA les liste, demande validation, puis exécute.

## 8. Décisions encore ouvertes (à confirmer avec David)

- Licence : GPL-3.0 (le code modifié doit rester open source) ou MIT (réutilisation libre). Proposition par défaut : **GPL-3.0**.
- Organisation GitHub : `kreafinity` ou `kreafinity-os`.
- Framework de l'interface web (vanilla vs Svelte/React).

## 9. Mainteneur

David LAW-DUNE — Kreafinity — contact@kreafinity.com
