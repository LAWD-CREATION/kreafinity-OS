# Interface web

L'interface de chat est servie par le démon (`kreafinity ui` l'ouvre dans le navigateur).
Elle est écrite en **HTML, CSS et JavaScript sans framework ni étape de compilation** : aucun
outil Node n'est nécessaire, et rien n'est chargé depuis Internet (polices, scripts, images) :
elle fonctionne hors ligne et ne divulgue rien à des tiers.

```
ui/web/
├── index.html          # squelette de la page
├── css/style.css       # styles ; couleurs Kreafinity en tête du fichier (variables CSS)
├── img/logo.svg
├── locales/fr.json     # textes de l'interface (français = référence)
├── locales/en.json
└── js/
    ├── main.js         # conversations, réponses en direct, cartes de validation
    ├── settings.js     # fenêtre de réglages (IA, clés API, outils, dossiers, général)
    ├── api.js          # appels REST et flux d'événements (WebSocket, reconnexion)
    ├── markdown.js     # rendu Markdown sûr des réponses
    ├── i18n.js         # traductions
    └── dom.js          # création d'éléments, icônes, dates relatives
```

L'API utilisée est décrite dans [api.md](api.md). Le paquet Python embarque ce dossier
(`kreafinity/web`) ; dans une copie du dépôt, le démon le sert directement depuis `ui/web`.

## Les pièces jointes

Le trombone (ou glisser-déposer un fichier, ou coller une image dans le champ de texte) joint
des fichiers au message : 10 fichiers au plus, 25 Mo chacun.

- **Images** : montrées à l'IA (réduites comme les captures d'écran), si son modèle lit les
  images ; sinon elle reçoit une note.
- **Textes** (notes, code, CSV, JSON…), **PDF**, **Word** (.docx) et **LibreOffice** (.odt, .ods,
  .odp) : leur texte est ajouté au message, encadré comme une donnée (jamais comme une
  instruction), 60 000 caractères au plus par fichier.
- Autres fichiers : seul leur nom est donné.

Les fichiers passent par `POST /api/uploads` (nom dans l'en-tête `X-Filename`) et restent un jour
dans `~/.local/share/kreafinity/uploads` (accessible à vous seul), puis le message les cite par
leur identifiant (`attachments`). Ils ne sont pas copiés dans vos dossiers. L'historique garde
leur nom et leur texte, pas les images.

## Règles de sécurité

Les textes affichés viennent de l'IA, de noms de fichiers ou de pages web : ce sont des données
non fiables.

- **Jamais de `innerHTML`**, `outerHTML`, `insertAdjacentHTML`, `document.write` ni `eval` :
  construisez les éléments avec `el()` (`dom.js`), qui insère le texte comme du texte.
- Le Markdown est rendu par `markdown.js`, qui crée les éléments un à un et n'accepte que des
  liens `http(s)`.
- Pas de script ni de style « en ligne » dans le HTML : la politique de sécurité (CSP) du démon
  les bloquerait. Les gestionnaires d'événements s'ajoutent en JavaScript.
- Une action `danger` doit afficher **toutes** ses cibles (`details`) et exiger une confirmation
  explicite (case à cocher, puis bouton).

Les tests `tests/test_web.py` vérifient ces règles sur le code, et `tests/test_ui.py` les vérifie
dans un vrai navigateur (une réponse contenant du HTML piégé reste du texte).

## Ajouter un texte

1. Ajoutez la clé dans `locales/fr.json` **et** `locales/en.json` (un test vérifie que les deux
   fichiers ont les mêmes clés et que toutes les clés utilisées existent).
2. Dans le HTML : `data-i18n="cle"` (texte), `data-i18n-placeholder`, `data-i18n-aria`.
   En JavaScript : `t("cle", { valeur })` ; pour un pluriel, `t("cle", { count })` avec les
   clés `cle.one` et `cle.other`.
3. Chaque outil a un nom (`tool.<nom>`) et une aide (`tool.<nom>.help`) affichés dans l'interface.

## Tester

```bash
pip install -e ".[dev]"
python -m playwright install chromium     # une fois
pytest tests/test_web.py tests/test_ui.py
```

`tests/test_ui.py` lance le vrai démon avec un faux Ollama scénarisé (`tests/fake_ollama.py`) et
pilote Chromium : scénario complet (lister, valider, mettre à la corbeille), refus, protection
contre le HTML piégé, historique et réglages. Ces tests sont ignorés si Playwright ou Chromium
manquent.

## La bulle de bureau

`kreafinity bubble` affiche un bouton rond en bas à droite du bureau (`ui/desktop/kreafinity_bubble.py`).
Un clic ouvre un panneau qui affiche cette même interface en **mode compact** (`/?compact=1` :
pas de liste de conversations visible ni de sélecteur d'IA, boutons « nouvelle conversation »,
« interface complète » et « fermer »).

- Programme autonome en Python + GTK 3 + WebKitGTK (paquets Debian/Ubuntu `python3-gi`,
  `gir1.2-gtk-3.0`, `gir1.2-webkit2-4.1`), lancé avec le Python du système qui fournit ces
  bibliothèques ; il ne dépend pas du cœur de Kreafinity et ne parle qu'au démon.
- La bulle reste au premier plan, sur tous les espaces de travail, hors de la barre des tâches ;
  on peut la déplacer en la faisant glisser ; clic droit : interface complète ou masquer la bulle.
- Le panneau est chargé dès le démarrage (caché) : il reçoit les événements du démon et la page
  signale à la bulle les actions en attente (pastille orange), par un pont WebKit
  (`window.webkit.messageHandlers.kreafinity`). Seuls trois messages existent (`attention`,
  `close`, `expand`) et le panneau n'affiche jamais d'autre page que l'interface du démon : les
  liens externes s'ouvrent dans le navigateur.
- Wayland ne laisse pas une application choisir sa position ni rester au premier plan : la bulle
  passe alors par XWayland (comme une application X11), ce qui fonctionne sous GNOME et KDE.
  Le bureau de l'étape 2 intégrera la bulle directement.
- `kreafinity bubble --autostart` crée `~/.config/autostart/kreafinity-bubble.desktop` pour
  l'afficher à chaque ouverture de session (`--no-autostart` pour l'enlever).
