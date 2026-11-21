# Parler à Kreafinity

Cliquez sur le **micro** à côté du champ de saisie, parlez, puis cliquez à nouveau : Kreafinity
écrit ce que vous avez dit, l'envoie, et vous répond **à voix haute**. Ça marche dans
l'interface web, dans le panneau ∞ de la barre des tâches (KDE Plasma) et dans la bulle.

![Le micro pendant l'écoute, puis la réponse](img/voix.jpg)

## Installer la voix

La voix est facultative : il faut environ **600 Mo** (moteurs et modèles), téléchargés une seule
fois. Deux façons :

- dans l'interface : **Réglages › Voix › Préparer la voix** (l'avancement s'affiche) ;
- en ligne de commande :

```bash
kreafinity voice install      # moteurs Whisper et Piper, modèle de reconnaissance, voix française
kreafinity voice              # état de la voix
```

## Sur l'ordinateur, sans rien envoyer

Par défaut, tout se passe **sur votre ordinateur**, même sans Internet :

- **Reconnaissance** : [Whisper](https://github.com/SYSTRAN/faster-whisper) (faster-whisper).
  Trois précisions au choix : rapide (140 Mo), équilibrée (460 Mo, par défaut), précise (1,5 Go,
  plus lente). Sur un processeur courant, une phrase de quelques secondes est comprise en 2 à
  4 secondes.
- **Lecture à voix haute** : une voix [Piper](https://github.com/OHF-Voice/piper1-gpl) (voix
  française « siwis », 60 Mo). La mise en forme est retirée avant la lecture : pas de « astérisque »
  ni de code lu à voix haute, et les adresses web sont sautées.

Les modèles sont rangés dans `~/.local/share/kreafinity/voice` ; l'enregistrement de votre voix
n'est gardé nulle part : il est transcrit puis oublié.

## Réglages

Dans **Réglages › Voix** (ou dans `~/.config/kreafinity/config.yaml`) :

| Réglage | Valeurs | Par défaut |
|---|---|---|
| Reconnaissance de la voix (`voice_input`) | `local` (sur l'ordinateur), `openai` (en ligne), `off` | `local` |
| Précision (`voice_model`) | `base`, `small`, `medium` | `small` |
| Réponses à voix haute (`voice_reply`) | `voice` (après une question posée à voix haute), `always`, `never` | `voice` |

Avec `openai`, l'enregistrement est envoyé à l'API de transcription d'OpenAI avec votre clé
OpenAI (celle du trousseau) : plus rapide sur un petit ordinateur, mais votre voix quitte la
machine. La lecture à voix haute reste toujours locale.

## Le micro

Le navigateur demande la permission d'utiliser le micro la première fois. Dans le panneau ∞ et
la bulle, Kreafinity l'autorise lui-même, **pour sa propre page uniquement** (jamais la caméra,
jamais une autre page). Un enregistrement dure au plus une minute.

## Pour les développeurs

- `core/kreafinity/voice/` : `audio.py` (décodage en 16 kHz mono avec PyAV), `stt.py`
  (Whisper local, API d'OpenAI), `tts.py` (Piper, texte à lire), `manager.py` (état,
  préparation en arrière-plan, utilisé par le démon).
- API : `GET /api/voice`, `POST /api/voice/prepare`, `POST /api/voice/transcribe` (le corps de
  la requête est l'enregistrement), `POST /api/voice/speak` (`{"text": …}` → `audio/wav`) ;
  événement `voice_updated`.
- Interface : `ui/web/js/voice.js` (MediaRecorder, lecture des réponses).
- Les dépendances sont l'extra `voice` du paquet (`pip install "kreafinity-os[voice]"`).
