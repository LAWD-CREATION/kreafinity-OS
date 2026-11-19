# Contribuer à Kreafinity OS

Merci de votre intérêt ! 🙌 *English speakers welcome — issues and PRs in English are fine.*

## Installer l'environnement de développement

```bash
git clone https://github.com/LAWD-CREATION/kreafinity-OS
cd kreafinity-OS
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pre-commit install
```

## Avant chaque commit

```bash
ruff check . && ruff format .
mypy
pytest
```

## Règles

- Code, noms et messages de commit en anglais ; textes de l'interface en français (traduisibles).
- Messages de commit au format [Conventional Commits](https://www.conventionalcommits.org/) : `feat:`, `fix:`, `docs:`, `chore:`…
- Un fournisseur LLM ou un outil = un fichier, sans toucher au reste.
- Jamais de clé API, de mot de passe ou de donnée personnelle dans le code, les tests ou les exemples.
- Toute action sensible de l'IA (suppression, commande système, installation) doit passer par la validation de l'utilisateur.

Le cahier des charges complet est dans [CLAUDE.md](CLAUDE.md).
