# ∞ Kreafinity OS

**Le système d'exploitation piloté par IA, où c'est vous qui choisissez votre IA.**
*The AI-driven operating system where you choose your AI.*

> 🚧 Projet en tout début de développement — *Early stage project*

---

## 🇫🇷 Français

### L'idée
Kreafinity OS est une distribution Linux open source dont l'intelligence artificielle est au cœur de l'expérience. Vous parlez à votre ordinateur pour lancer des applications, ranger vos fichiers ou régler votre système, dans une interface familière inspirée de Windows.

### Pourquoi Kreafinity OS ?
- 🔓 **Liberté du fournisseur IA** : Claude, GPT, Mistral ou un modèle 100 % local avec Ollama. Vous changez quand vous voulez.
- 🛡️ **Vous gardez le contrôle** : aucune action sensible (suppression, commande système, installation) sans votre validation.
- 🔒 **Vie privée** : fonctionne entièrement hors ligne avec un modèle local.
- 🪟 **Interface familière** : barre des tâches, menu démarrer, fenêtres… avec l'IA intégrée partout.
- 🌍 **Open source** : construit sur Linux (Debian), ouvert aux contributions.

### Feuille de route
- [ ] **Étape 1** — Assistant système multi-LLM (démon, CLI, interface web de chat)
- [ ] **Étape 2** — Bureau façon Windows avec IA intégrée
- [ ] **Étape 3** — Distribution bootable (ISO, clé USB live, installateur)

### Essayer (version de développement)
```bash
git clone https://github.com/LAWD-CREATION/kreafinity-OS && cd kreafinity-OS
python3 -m venv .venv && source .venv/bin/activate && pip install -e .

# Avec un modèle local (https://ollama.com) :
ollama pull llama3.1
kreafinity ask "Quels sont les 5 plus gros fichiers de mon dossier Téléchargements ?"

# Avec une IA en ligne (clés bientôt stockées dans le trousseau système) :
export ANTHROPIC_API_KEY=...   # Claude      → kreafinity chat -p anthropic
export OPENAI_API_KEY=...      # GPT         → kreafinity chat -p openai
export MISTRAL_API_KEY=...     # Mistral AI  → kreafinity chat -p mistral
```

Changer d'IA :
```bash
kreafinity provider list                  # fournisseurs disponibles
kreafinity provider models mistral        # modèles d'un fournisseur
kreafinity provider use mistral/mistral-large-latest   # fournisseur par défaut
```
Dans `kreafinity chat`, tapez `/fournisseur anthropic` pour changer d'IA **sans perdre la conversation**.
Pour basculer automatiquement sur un modèle local quand Internet ne répond pas, ajoutez
`fallback: ollama/llama3.1` dans `~/.config/kreafinity/config.yaml`.

Toute action qui modifie l'ordinateur (créer, déplacer, ouvrir, mettre à la corbeille…) vous est
d'abord présentée et ne s'exécute qu'avec votre accord. Journal des actions :
`~/.local/share/kreafinity/actions.log`.

Historique : `kreafinity history` liste vos conversations, `kreafinity chat --reprendre <id>`
en reprend une.

Démon (API locale pour l'interface web, sur 127.0.0.1 uniquement, protégée par un jeton) :
```bash
kreafinity daemon      # voir docs/api.md
```

### Contribuer
Le projet démarre : idées, retours et contributions sont les bienvenus via les *issues* et *pull requests*.

---

## 🇬🇧 English

### The idea
Kreafinity OS is an open source Linux distribution with artificial intelligence at the heart of the experience. Talk to your computer to launch apps, organize files or adjust settings, in a familiar Windows-inspired desktop.

### Why Kreafinity OS?
- 🔓 **Freedom of AI provider**: Claude, GPT, Mistral or a fully local model via Ollama. Switch anytime.
- 🛡️ **You stay in control**: no sensitive action (deletion, shell command, install) without your approval.
- 🔒 **Privacy**: runs fully offline with a local model.
- 🪟 **Familiar interface**: taskbar, start menu, windows… with AI built in everywhere.
- 🌍 **Open source**: built on Linux (Debian), open to contributions.

### Roadmap
- [ ] **Step 1** — Multi-LLM system assistant (daemon, CLI, web chat UI)
- [ ] **Step 2** — Windows-like desktop shell with built-in AI
- [ ] **Step 3** — Bootable distribution (ISO, live USB, installer)

### Try it (development version)
```bash
git clone https://github.com/LAWD-CREATION/kreafinity-OS && cd kreafinity-OS
python3 -m venv .venv && source .venv/bin/activate && pip install -e .
ollama pull llama3.1                      # local model, see https://ollama.com
kreafinity ask "What are the 5 largest files in my Downloads folder?"
kreafinity chat --provider anthropic      # with ANTHROPIC_API_KEY set (or openai, mistral)
kreafinity provider use mistral           # change the default provider
```
In `kreafinity chat`, `/provider openai` switches AI without losing the conversation.
`kreafinity history` lists past conversations, `kreafinity chat --resume <id>` resumes one, and
`kreafinity daemon` starts the local API used by the web interface (see `docs/api.md`).
Set `language: en` in `~/.config/kreafinity/config.yaml` for English answers, and
`fallback: ollama/llama3.1` to switch to a local model when offline.

---

## Licence / License
[GPL-3.0](LICENSE)

## Auteur / Author
Créé à La Réunion 🌋 par **David LAW-DUNE** — [Kreafinity](https://kreafinity.com), agence web & IA.
Contact : contact@kreafinity.com
