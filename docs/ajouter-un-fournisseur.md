# Ajouter un fournisseur LLM

Un fournisseur (« provider ») est un adaptateur entre le format **neutre** de Kreafinity
et l'API d'un service d'IA. Le reste du système (agent, historique, CLI, interface web)
ne connaît que le format neutre : c'est ce qui permet de changer de fournisseur en
pleine conversation.

## Le format neutre (`kreafinity/providers/base.py`)

| Type | Rôle |
|---|---|
| `Message` | Un message : `role` (`system`, `user`, `assistant`, `tool`), `content`, `tool_calls`, `tool_call_id`, `name`, `is_error`, `provider_data` |
| `ToolCall` | Un appel d'outil demandé par le modèle : `id`, `name`, `arguments` (dict JSON) |
| `ToolSpec` | Un outil proposé au modèle : `name`, `description`, `parameters` (JSON Schema) |
| `TextDelta` | Un morceau de texte reçu en streaming |
| `ResponseDone` | Dernier événement : le message assistant complet, `stop_reason`, `model`, `usage` |
| `ModelInfo` | Un modèle proposé par le fournisseur |

Le résultat d'un outil est un message `role="tool"` qui reprend l'`id` de l'appel.
Les outils ne doivent être exécutés que si `stop_reason == "tool_use"`.

`provider_data` permet à un adaptateur de stocker des données propres à son API sur
les messages qu'il produit (ex. les blocs de réflexion de Claude, qui doivent être
renvoyés tels quels). Il les relit quand la conversation lui revient ; les autres
fournisseurs les ignorent.

## Écrire l'adaptateur

Créez **un seul fichier** dans `core/kreafinity/providers/`, par exemple `monia.py` :

```python
from collections.abc import AsyncIterator, Sequence

from kreafinity.providers.base import (
    LLMProvider, Message, ModelInfo, ResponseDone, StreamEvent, TextDelta, ToolSpec,
)
from kreafinity.providers.registry import register_provider


@register_provider
class MonIAProvider(LLMProvider):
    name = "monia"                 # utilisé dans la config et la CLI : monia/mon-modele
    display_name = "Mon IA"
    is_local = False               # True si le modèle tourne sur la machine

    def __init__(self, *, api_key: str | None = None, default_model: str = "mon-modele") -> None:
        super().__init__(default_model=default_model)
        ...

    async def chat(
        self, messages: Sequence[Message], tools: Sequence[ToolSpec] = (), *, model: str | None = None
    ) -> AsyncIterator[StreamEvent]:
        # 1. convertir `messages` et `tools` vers le format de l'API
        # 2. appeler l'API en streaming et émettre des TextDelta
        # 3. terminer par exactement un ResponseDone
        ...

    async def list_models(self) -> list[ModelInfo]:
        ...
```

Le fichier est découvert automatiquement : aucun autre fichier n'est à modifier.

### Erreurs

Traduisez les erreurs de l'API en :

- `ProviderUnavailableError` : service injoignable (pas de réseau, serveur local arrêté).
  Le système peut alors basculer vers un autre fournisseur (ex. Ollama hors ligne).
- `ProviderAuthError` : clé API absente ou refusée.
- `ProviderError(retryable=...)` : toute autre erreur ; `retryable=True` si réessayer
  plus tard peut fonctionner (limite de débit, surcharge).

### Points d'attention

- Tous les résultats d'outils d'un même tour doivent être renvoyés ensemble si l'API
  l'exige (c'est le cas d'Anthropic).
- Si l'API ne fournit pas d'identifiant d'appel d'outil, utilisez `new_tool_call_id()`.
- Les arguments d'outils viennent du modèle : ce sont des données non fiables, validées
  ensuite par la couche outils.

## Fournisseur externe (plugin)

Un paquet tiers peut ajouter un fournisseur sans toucher au dépôt, via un point d'entrée :

```toml
[project.entry-points."kreafinity.providers"]
monia = "monia_kreafinity:MonIAProvider"
```

## Tests

Aucun appel réseau réel dans les tests. Simulez l'API au niveau HTTP :
`httpx.MockTransport` (voir `tests/test_provider_ollama.py`) ou, pour les SDK basés sur
`httpx2`, `httpx2.MockTransport` (voir `tests/test_provider_anthropic.py`). Testez au
minimum : texte en streaming, appels d'outils, conversion de l'historique (y compris un
historique produit par un autre fournisseur) et chaque type d'erreur.
