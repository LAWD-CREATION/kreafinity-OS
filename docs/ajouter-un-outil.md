# Ajouter un outil

Un outil est une action que l'IA peut effectuer sur la machine. Chaque outil déclare un
**niveau de risque**, qui décide si l'utilisateur doit valider chaque appel :

| Niveau | Comportement |
|---|---|
| `RiskLevel.SAFE` | Exécuté directement (lecture seule : infos, lister, rechercher) |
| `RiskLevel.CONFIRM` | Validation de l'utilisateur avant exécution (créer, déplacer, ouvrir…) |
| `RiskLevel.DANGER` | Validation explicite avec affichage de l'action exacte (supprimer, shell…) |

L'agent applique ces règles lui-même : un outil `confirm` ou `danger` n'est **jamais**
exécuté sans l'accord de l'utilisateur, quel que soit le fournisseur d'IA.

## Écrire l'outil

Créez **un seul fichier** dans `core/kreafinity/tools/`, par exemple `disk_usage.py` :

```python
from typing import Any, ClassVar

from kreafinity.tools.base import RiskLevel, Tool, ToolContext, ToolError, ToolResult
from kreafinity.tools.registry import register_tool


@register_tool
class DiskUsage(Tool):
    name = "disk_usage"
    description = "Compute the total size of a folder."  # pour le modèle, en anglais
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Folder to measure."}},
        "required": ["path"],
        "additionalProperties": False,
    }
    risk = RiskLevel.SAFE

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        folder = context.resolve(arguments["path"])  # refuse tout chemin hors du bac à sable
        if not folder.is_dir():
            raise ToolError(f"Not a folder: {folder}")
        ...
        return ToolResult.json({"path": str(folder), "size_bytes": total})
```

Le fichier est découvert automatiquement ; un fichier dont le nom commence par `_` est
ignoré (utile pour du code partagé, comme `_fs.py`).

## Règles

- **Arguments** : ils sont validés par l'agent avec le JSON Schema `parameters` avant
  l'appel, mais restent des données non fiables produites par le modèle.
- **Chemins** : passez toujours par `context.resolve()`. Il interprète `~`, résout les
  liens symboliques et refuse tout ce qui sort des dossiers autorisés (dossier personnel
  par défaut) ou touche aux dossiers sensibles (`~/.ssh`, `~/.gnupg`, trousseaux…).
  Pour parcourir une arborescence, ne suivez pas les liens symboliques et vérifiez chaque
  entrée avec `context.is_allowed()`.
- **Erreurs attendues** : levez `ToolError("message")` ; le message est transmis au modèle,
  qui peut corriger sa demande. Une autre exception est rapportée comme erreur interne.
- **Opérations bloquantes** (disque, mesures) : exécutez-les avec `asyncio.to_thread()`
  pour ne pas bloquer le démon.
- **Sortie** : préférez `ToolResult.json(...)`, avec des données compactes. Au-delà de
  20 000 caractères, la sortie est tronquée.
- **Suppression** : jamais de `rm` ; envoi à la corbeille (spécification freedesktop).
- **Root** : jamais de `sudo` ; passez par `pkexec`/polkit.
- Chaque appel est inscrit dans le journal `~/.local/share/kreafinity/actions.log`.

## Outil externe (plugin)

```toml
[project.entry-points."kreafinity.tools"]
disk_usage = "mon_paquet.outils:DiskUsage"
```

## Tests

Utilisez les fixtures `home` (faux dossier personnel avec quelques fichiers) et
`context` de `tests/conftest.py`. Testez au minimum le cas normal, les erreurs, et qu'un
chemin hors du bac à sable (`/etc`, `~/..`, un lien symbolique vers l'extérieur) est refusé.
