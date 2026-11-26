# Kreafinity OS — Passation pour Gemini (Antigravity)

> Ce fichier permet à un agent (Gemini dans Antigravity, ou tout autre) de reprendre le projet
> là où il en est. **Lire aussi [`CLAUDE.md`](CLAUDE.md)** : c'est le brief du projet (vision,
> périmètre, règles de sécurité). Ses règles s'appliquent à tout agent, pas seulement à Claude.
> En cas de doute, la documentation de `docs/` et le code font foi.

---

## 1. En une phrase

Kreafinity OS est une distribution Linux (Debian 13 + KDE Plasma) dont l'assistant IA est le
cœur : on lui parle (texte ou voix) pour gérer ses fichiers, ses applications et son système,
avec le fournisseur d'IA de son choix (Claude, GPT, Mistral, Ollama local, Ollama Cloud,
OpenRouter). Toute action sensible demande la validation de l'utilisateur.

## 2. Les « connexions » : où est quoi

| Quoi | Où |
|---|---|
| Dépôt GitHub | https://github.com/LAWD-CREATION/kreafinity-OS (branche principale : `main`) |
| Versions publiées (mises à jour des PC installés) | https://github.com/LAWD-CREATION/kreafinity-OS/releases |
| Intégration continue | https://github.com/LAWD-CREATION/kreafinity-OS/actions (`ci.yml`, `apt.yml`, `iso.yml`) |
| Dépôt APT signé (facultatif, pas encore publié) | https://lawd-creation.github.io/kreafinity-OS/ (voir § 7) |
| Démon local (sur le PC de l'utilisateur) | `http://127.0.0.1:27494` (127.0.0.1 uniquement) |
| Jeton d'accès local à l'API | `~/.local/share/kreafinity/token` (lisible par l'utilisateur seul) |
| Configuration | `~/.config/kreafinity/config.yaml` |
| Clés API des fournisseurs | trousseau système (Secret Service, bibliothèque `keyring`), **jamais** dans un fichier |
| Données (historique SQLite, mémoire, journal, téléversements) | `~/.local/share/kreafinity/` |
| Journal de toutes les actions exécutées | `~/.local/share/kreafinity/actions.log` |
| Paquet installé (Kreafinity OS / paquet Debian) | `/opt/kreafinity` (son propre Python 3.13), commandes `kreafinity` et `kreafinity-core` |
| Service du démon | `kreafinity.service` (systemd utilisateur) |
| Widget de la barre des tâches KDE (Meta+C) | `org.kreafinity.assistant` (`ui/plasma/plasmoid-6`, et `-5` pour Plasma 5) |

Aucun secret n'est nécessaire pour développer, tester ou publier une mise à jour.

## 3. Mettre en place l'environnement de développement

```bash
git clone https://github.com/LAWD-CREATION/kreafinity-OS.git
cd kreafinity-OS
python3 -m venv .venv && . .venv/bin/activate      # Python 3.11 ou plus
pip install -e ".[dev]"
playwright install chromium                          # tests de l'interface web
pre-commit install
```

Avant **chaque** commit :

```bash
ruff check core tests && ruff format --check core tests
mypy core
pytest -q                                            # ≈ 630 tests, ≈ 4 min
```

- Les fournisseurs d'IA sont **simulés** dans les tests (`tests/fakes.py`, `tests/fake_ollama.py`) :
  aucun appel réseau réel vers une IA.
- Les tests d'interface lancent le vrai démon dans un sous-processus et un vrai Chromium
  (Playwright).
- Lancer le démon à la main : `kreafinity-core --port 27494`, puis ouvrir http://127.0.0.1:27494.
  En ligne de commande : `kreafinity ask "…"`, `kreafinity chat`.

## 4. Architecture

```
core/kreafinity/
├── providers/   un fichier par fournisseur (anthropic, openai, mistral, ollama, ollama_cloud,
│                openrouter) + base.py (format neutre des messages, images) + registry.py
├── agent/       loop.py (boucle agent : LLM → outils → validation → LLM), prompts.py,
│                history.py (SQLite), action_log.py, review.py (mémoire après conversation)
├── tools/       un fichier par outil, chacun avec son niveau de risque (safe/confirm/danger),
│                registry.py ; base.py = contexte (dossiers autorisés, dossiers protégés)
├── api/         app.py (FastAPI : REST + WebSocket), auth.py (jeton), server.py (démon), web.py
├── service.py   le cœur du démon : conversations, exécutions, validations, événements en direct
├── config/      réglages (settings.py) + trousseau
├── permissions.py   « Toujours autoriser » (règles choisies par l'utilisateur sur une carte)
├── update.py    mises à jour (GitHub Releases pour le paquet ; voir § 6)
├── attachments.py   fichiers joints (texte, PDF, docx/odt, images)
├── mcp/         client MCP (extensions : outils de serveurs Model Context Protocol)
├── voice/       dictée (Whisper local) et voix (Piper)
├── browser/     navigateur de l'assistant (WebDriver BiDi / CDP)
├── pilot/       mode pilote (souris et clavier, avec bandeau et Échap pour reprendre la main)
├── plasma/      intégration KDE (KRunner, menu Dolphin)
├── system/      intégration système (corbeille, applications, fenêtres, son, luminosité,
│                paquets via pkexec, fenêtre de choix de fichiers…)
├── memory.py, onboarding.py (accueil au premier démarrage), i18n.py + locales/
└── cli.py       commandes `kreafinity …` (Typer + Rich)

ui/web/          interface web (HTML/CSS/JS sans framework), traductions dans ui/web/locales/
ui/plasma/       widgets KDE (QML) ; ui/desktop/ : bulle GTK, bandeau du mode pilote
packaging/       debian/ (paquet .deb), release/ (publication GitHub Releases), apt/ (dépôt
                 APT signé), iso/ (image live-build), install.sh, kreafinity.service
docs/            documentation utilisateur et développeur (en français)
tests/           pytest (unitaires, API, navigateur réel)
```

Guides : `docs/ajouter-un-fournisseur.md`, `docs/ajouter-un-outil.md`, `docs/api.md`,
`docs/outils.md`, `docs/distribution.md`, `docs/mcp.md`, `docs/voix.md`, `docs/memoire.md`.

## 5. Règles à respecter (non négociables)

- **Aucune** action `confirm`/`danger` sans validation de l'utilisateur, quel que soit le
  fournisseur — sauf ce que l'utilisateur a lui-même autorisé pour de bon (« Toujours
  autoriser », liste dans Réglages › Autorisations). L'IA ne s'accorde jamais ces droits.
- Suppression = corbeille freedesktop, jamais `rm`. Jamais de `sudo` automatique : `pkexec`.
- Accès fichiers limité au dossier personnel par défaut ; `~/.config/kreafinity`, `.ssh`, les
  trousseaux… restent inaccessibles aux outils de fichiers.
- Démon sur 127.0.0.1 uniquement, avec jeton local.
- Clés API dans le trousseau, jamais en clair (ni config, ni Git, ni tests).
- Code, noms et commits **en anglais** (Conventional Commits : `feat:`, `fix:`, `test:`…) ;
  textes de l'interface **en français** (i18n : `ui/web/locales/`, `core/kreafinity/locales/`).
- Un fournisseur = un fichier ; un outil = un fichier.
- Toujours faire tourner lint, types et tests avant un commit ; ne jamais désactiver un test
  pour obtenir du vert.

## 6. Publier une mise à jour (ce que reçoivent les PC installés)

1. Travailler sur une branche, ouvrir une Pull Request vers `main`.
2. La CI (`ci.yml`) lance lint, tests (Python 3.11, 3.12, 3.13) et construit/installe le paquet.
3. Fusionner quand tout est vert.
4. À chaque changement de `main`, `apt.yml` construit le paquet et le publie dans les
   **Releases GitHub** (`packaging/release/publish.sh`, avec son empreinte SHA-256 ; les
   10 dernières versions sont gardées). Aucune clé ni secret n'est nécessaire.
5. Sur le PC de l'utilisateur, Kreafinity cherche une nouvelle version chaque jour
   (ou Réglages › Général › « Rechercher maintenant »), puis le bouton « Mettre à jour »
   télécharge le paquet, vérifie l'empreinte et l'installe avec `pkexec apt-get install`
   (le système demande le mot de passe), puis redémarre le démon.
   - Une version plus ancienne que celle installée n'est jamais proposée.
   - Le widget KDE (QML) n'est rechargé qu'à la prochaine ouverture de session.

Construire à la main : `packaging/debian/build-deb.sh dist` (le paquet) ;
l'ISO : GitHub › Actions › ISO › Run workflow (voir `docs/distribution.md`).

## 7. État actuel (26 novembre 2026)

**Fait et en service sur le PC de l'utilisateur** (Kreafinity OS installé depuis l'ISO, Debian 13
+ KDE Plasma 6, mises à jour par le bouton « Mettre à jour ») :
- étape 1 du brief : démon, CLI, interface web, 6 fournisseurs, outils avec niveaux de risque,
  validation, historique, réglages en onglets, trousseau ;
- étapes 2 et 3 avancées : thème KDE façon Windows, widget Meta+C, recherche KRunner, menu
  Dolphin, mémoire et « âme », voix (dictée + lecture + conversation orale), fichiers joints,
  navigateur de l'assistant, mode pilote, extensions MCP, accueil au premier démarrage, paquet
  Debian, ISO live + installateur Calamares, mises à jour par GitHub Releases ;
- dernières additions : « Toujours autoriser », réglages en onglets, trombone et agrandissement
  du widget KDE passant par le démon (`/api/uploads/pick`, `/api/ui/open-full`), option
  « Sans limite » pour le nombre d'étapes d'une demande.

**Reste à faire / pistes** :
- **Dépôt APT signé** (facultatif, les Releases suffisent) : le secret GitHub `APT_SIGNING_KEY`
  contient une clé *publique* au lieu de la clé privée, donc le dépôt APT n'est pas publié
  (`apt.yml` le signale par un avertissement, sans échouer). Pour l'activer : la clé privée
  complète (`-----BEGIN PGP PRIVATE KEY BLOCK-----`), envoyée par
  `gh secret set APT_SIGNING_KEY --repo LAWD-CREATION/kreafinity-OS < ~/kreafinity-apt-signing-key.asc`.
  Ne jamais mettre une clé privée dans le dépôt ni dans une conversation.
- Vérifier sur le vrai PC : le portail Wayland du mode pilote, la conversation orale avec de
  vrais haut-parleurs (écho), Secure Boot de l'ISO, l'installation à côté de Windows.
- Décisions encore ouvertes du brief (§ 8 de `CLAUDE.md`) : licence, organisation GitHub.

## 8. Façon de travailler avec l'utilisateur

- L'utilisateur (le mainteneur) écrit en français et n'est pas développeur : réponses courtes,
  sans jargon, étapes numérotées, une seule commande à copier quand c'est inévitable.
  Il préfère que tout se fasse sans terminal (bouton « Mettre à jour »).
- Expliquer ce qui a été essayé pour de vrai et ce qui ne l'a pas été.
- Ne fusionner une Pull Request qu'avec son accord explicite, et seulement quand la CI est verte.
- Ne jamais lui demander de coller un secret dans la conversation.

Mainteneur : Kreafinity — contact@kreafinity.com — https://kreafinity.com
