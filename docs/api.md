# API locale du démon `kreafinity-core`

Le démon expose l'assistant aux interfaces (interface web, futur bureau) par une API
**REST** pour les actions et un **WebSocket** pour suivre les réponses en direct.

```bash
kreafinity daemon            # ou : kreafinity-core [--port 27494]
```

- Écoute **uniquement sur `127.0.0.1`**, port `27494` par défaut (réglage `port`).
- `~/.local/share/kreafinity/daemon.json` (lisible par vous seul) indique l'adresse et le
  PID du démon en cours d'exécution.
- Historique des conversations : `~/.local/share/kreafinity/history.db` (SQLite), partagé
  avec la CLI (`kreafinity history`, `kreafinity chat --reprendre <id>`).

## Authentification

Toute requête, sauf `GET /api/health`, doit présenter le **jeton local**, stocké dans
`~/.local/share/kreafinity/token` (créé au premier lancement, droits `0600`, dans un dossier
inaccessible aux outils de l'IA).

- REST : en-tête `Authorization: Bearer <jeton>`. Sans jeton valide : `401`.
- WebSocket : le **premier message** doit être `{"type": "auth", "token": "<jeton>"}`,
  envoyé dans les 10 secondes ; sinon la connexion est fermée (code 1008).

Protections supplémentaires :

- Aucun cookie : un autre site web ouvert dans le navigateur ne peut pas agir à votre place
  (pas de CSRF), et aucune en-tête CORS n'est envoyée.
- L'en-tête `Host` doit être `127.0.0.1` ou `localhost` (protection contre le *DNS rebinding*).
- Un WebSocket ouvert depuis une page web doit venir de `http://127.0.0.1:<port>` ou
  `http://localhost:<port>` (en-tête `Origin`).

```bash
TOKEN=$(cat ~/.local/share/kreafinity/token)
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:27494/api/conversations
```

## Routes REST

| Méthode | Chemin | Rôle |
|---|---|---|
| `GET` | `/api/health` | État du démon (sans jeton) |
| `GET` | `/api/conversations?limit=50&offset=0` | Conversations, les plus récentes d'abord |
| `POST` | `/api/conversations` | Nouvelle conversation (`{"title": "…"}` facultatif) → `201` |
| `GET` | `/api/conversations/{id}` | Une conversation et ses messages |
| `PATCH` | `/api/conversations/{id}` | Renommer : `{"title": "…"}` |
| `DELETE` | `/api/conversations/{id}` | Supprimer (arrête la demande en cours) → `204` |
| `POST` | `/api/conversations/{id}/messages` | Envoyer un message → `202 {"run_id", "provider", "model"}` |
| `POST` | `/api/conversations/{id}/cancel` | Arrêter la demande en cours |
| `GET` | `/api/approvals` | Actions en attente de validation |
| `POST` | `/api/approvals/{approval_id}` | Décision : `{"approved": true}` ou `false` |
| `GET` | `/api/providers` | Fournisseurs, fournisseur par défaut et de secours |
| `GET` | `/api/providers/{nom}/models` | Modèles d'un fournisseur |
| `PUT` | `/api/providers/default` | Fournisseur par défaut : `{"provider": "mistral/mistral-large-latest"}` |
| `GET` | `/api/tools` | Outils, niveau de risque, activé ou non |
| `GET` | `/api/config` | Réglages |
| `PATCH` | `/api/config` | Modifier des réglages (validés ensemble) : `{"language": "en", …}` |

Envoyer un message accepte aussi `"provider": "anthropic/claude-opus-5-5"` : la conversation
continue avec ce fournisseur, qui est mémorisé pour elle. Une seule demande à la fois par
conversation (sinon `409`).

Erreurs : `{"error": {"code": "…", "message": "…"}}` avec les codes `not_found` (404),
`busy` (409), `invalid` (400), `invalid_config` (409), `provider_unavailable` (503),
`provider_auth` et `provider_error` (502). Les erreurs de format renvoient `422`.

## Événements (WebSocket `/api/events`)

Après l'authentification, le démon envoie `ready` (avec les demandes en cours et les
validations en attente), puis tous les événements, numérotés dans l'ordre (`seq`).

| `type` | Contenu principal |
|---|---|
| `run_started` | `conversation_id`, `run_id`, `provider`, `provider_name`, `model`, `content` (message de l'utilisateur) |
| `text_delta` | `text` : morceau de la réponse |
| `tool_started` | `call_id`, `tool`, `arguments`, `description`, `risk` (`safe`/`confirm`/`danger`) |
| `approval_required` | `approval_id`, `tool`, `risk`, `summary`, `details` (cibles exactes), `arguments` |
| `approval_resolved` | `approval_id`, `approved`, `reason` (`user` ou `timeout`) |
| `tool_finished` | `call_id`, `status` (`ok`/`error`/`denied`/`skipped`), `is_error`, `result` (extrait) |
| `provider_fallback` | `unavailable`, `fallback` : bascule vers le fournisseur de secours |
| `run_finished` | `reason` : `completed`, `iteration_limit`, `max_tokens`, `refusal`, `cancelled`… |
| `run_failed` | `code` (`provider_unavailable`, `provider_auth`…), `message` |
| `conversation_updated` / `conversation_deleted` | Conversation modifiée (titre, état…) / supprimée |
| `config_updated` | Nouveaux réglages |
| `resync_required` | Client trop lent : se reconnecter et recharger l'état |

Les événements des demandes portent `conversation_id` et `run_id`.

> **Affichage** : les textes reçus (réponses, `arguments`, `details`, résultats d'outils,
> titres) viennent de l'IA ou de noms de fichiers. Une interface doit toujours les afficher
> comme du texte (`textContent`), jamais les interpréter comme du HTML.

### Validation des actions

Quand l'IA veut exécuter un outil `confirm` ou `danger`, le démon envoie
`approval_required` et **attend la décision** : rien n'est exécuté avant
`POST /api/approvals/{approval_id}` avec `{"approved": true}` (un vrai booléen ; `"yes"` est
refusé). Sans réponse au bout de 10 minutes, l'action est refusée. Pour un outil `danger`,
l'interface doit afficher `summary` et **tous** les `details` et demander une confirmation
explicite.

### Exemple (Python)

```python
import json, pathlib, httpx
from websockets.sync.client import connect

token = pathlib.Path("~/.local/share/kreafinity/token").expanduser().read_text().strip()
api = httpx.Client(base_url="http://127.0.0.1:27494", headers={"Authorization": f"Bearer {token}"})

with connect("ws://127.0.0.1:27494/api/events") as ws:
    ws.send(json.dumps({"type": "auth", "token": token}))
    json.loads(ws.recv())  # ready
    conversation = api.post("/api/conversations").json()
    api.post(f"/api/conversations/{conversation['id']}/messages", json={"content": "Bonjour"})
    while True:
        event = json.loads(ws.recv())
        if event["type"] == "text_delta":
            print(event["text"], end="", flush=True)
        elif event["type"] == "approval_required":
            print("\n", event["summary"], *event["details"], sep="\n  ")
            approved = input("Autoriser ? [o/N] ").strip().lower() == "o"
            api.post(f"/api/approvals/{event['approval_id']}", json={"approved": approved})
        elif event["type"] in ("run_finished", "run_failed"):
            break
```
