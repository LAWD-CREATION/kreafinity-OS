#!/usr/bin/env bash
# Installe Kreafinity pour l'utilisateur courant (Debian, Ubuntu et dérivées).
#
#   curl -fsSL https://raw.githubusercontent.com/LAWD-CREATION/kreafinity-OS/main/packaging/install.sh | bash
#
# Kreafinity s'installe dans votre dossier personnel, sans droits administrateur :
# « sudo » n'est demandé que pour installer des paquets système, après accord.
#
# Options :
#   --yes, -y                 répondre oui à toutes les questions
#   --provider=NOM            IA à configurer : ollama, ollama-cloud, anthropic, openai,
#                             mistral, openrouter, later
#   --no-system-packages      ne pas installer de paquets système (apt)
#   --no-bubble               ne pas afficher la bulle sur le bureau
#   --no-plasma               ne pas intégrer Kreafinity à KDE Plasma (recherche, widget, thème)
#   --allow-root              autoriser l'installation pour root (conteneurs de test)
#   --help, -h                afficher cette aide
#
# Variables : KREAFINITY_REF (branche ou étiquette, « main » par défaut),
#             KREAFINITY_SOURCE (dossier ou archive à installer à la place),
#             KREAFINITY_OLLAMA_MODEL (modèle local, « llama3.1 » par défaut).

set -euo pipefail

REPOSITORY="https://github.com/LAWD-CREATION/kreafinity-OS"
REF="${KREAFINITY_REF:-main}"
SOURCE="${KREAFINITY_SOURCE:-$REPOSITORY/archive/$REF.tar.gz}"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/kreafinity"
VENV="$DATA_DIR/venv"
BIN_DIR="$HOME/.local/bin"
OLLAMA_MODEL="${KREAFINITY_OLLAMA_MODEL:-llama3.1}"

assume_yes=0
provider=""
system_packages=1
bubble=1
allow_root=0
plasma_integration=1

if [[ -t 1 ]]; then
  BOLD=$'\033[1m' VIOLET=$'\033[1;35m' YELLOW=$'\033[1;33m' RED=$'\033[1;31m' RESET=$'\033[0m'
else
  BOLD="" VIOLET="" YELLOW="" RED="" RESET=""
fi

say() { printf '%s∞%s %s\n' "$VIOLET" "$RESET" "$*"; }
warn() { printf '%s!%s %s\n' "$YELLOW" "$RESET" "$*" >&2; }
die() {
  printf '%s✗%s %s\n' "$RED" "$RESET" "$*" >&2
  exit 1
}

usage() {
  if [[ -f "${BASH_SOURCE[0]:-}" ]]; then
    sed -n '2,19p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  else # read from a pipe (curl … | bash -s -- --help)
    echo "Options : --yes, --provider=NOM, --no-system-packages, --no-bubble, --no-plasma, --allow-root"
    echo "Aide complète : $REPOSITORY/blob/main/docs/installation.md"
  fi
  exit 0
}

for argument in "$@"; do
  case "$argument" in
    --yes | -y) assume_yes=1 ;;
    --provider=*) provider="${argument#--provider=}" ;;
    --no-system-packages) system_packages=0 ;;
    --no-bubble) bubble=0 ;;
    --no-plasma) plasma_integration=0 ;;
    --allow-root) allow_root=1 ;;
    --help | -h) usage ;;
    *) die "Option inconnue : $argument (voir --help)" ;;
  esac
done

case "$provider" in
  "" | ollama | ollama-cloud | anthropic | openai | mistral | openrouter | later) ;;
  *) die "IA inconnue : $provider (ollama, ollama-cloud, anthropic, openai, mistral, openrouter ou later)" ;;
esac

# Questions are read from the terminal, even when this script comes through a pipe.
has_tty() { { : </dev/tty; } 2>/dev/null; }

ask() {
  local answer=""
  if ((assume_yes)) || ! has_tty; then
    return 0
  fi
  printf '%s?%s %s [O/n] ' "$BOLD" "$RESET" "$1" >/dev/tty
  read -r answer </dev/tty || answer=""
  [[ -z "$answer" || "$answer" =~ ^[oOyY] ]]
}

# The same, for changes the user must choose explicitly: "no" by default and with --yes.
ask_no() {
  local answer=""
  if ((assume_yes)) || ! has_tty; then
    return 1
  fi
  printf '%s?%s %s [o/N] ' "$BOLD" "$RESET" "$1" >/dev/tty
  read -r answer </dev/tty || answer=""
  [[ "$answer" =~ ^[oOyY] ]]
}

# --- Checks -------------------------------------------------------------------------

[[ "$(uname -s)" == "Linux" ]] || die "Kreafinity fonctionne sous Linux."
if ((EUID == 0 && !allow_root)); then
  die "Lancez ce script sans sudo : il installe Kreafinity pour votre compte et demande sudo seulement pour les paquets système."
fi

say "Installation de Kreafinity pour ${USER:-$(id -un)}"

find_python() {
  local candidate path
  for candidate in python3 python3.13 python3.12 python3.11; do
    path="$(command -v "$candidate" 2>/dev/null)" || continue
    if "$path" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
      printf '%s\n' "$path"
      return 0
    fi
  done
  return 1
}

installed() { dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "install ok installed"; }
available() { apt-cache show "$1" >/dev/null 2>&1; }

# Current commit of a branch on GitHub ("owner/name" "branch"); empty if unknown.
latest_commit() {
  "$VENV/bin/python" -c '
import sys, urllib.request
request = urllib.request.Request(
    "https://api.github.com/repos/%s/commits/%s" % (sys.argv[1], sys.argv[2]),
    headers={"Accept": "application/vnd.github.sha", "User-Agent": "Kreafinity"},
)
commit = urllib.request.urlopen(request, timeout=20).read().decode().strip()
if len(commit) == 40 and all(char in "0123456789abcdef" for char in commit):
    print(commit)
' "$1" "$2" 2>/dev/null || true
}

# --- System packages (apt) ----------------------------------------------------------------

python="$(find_python || true)"
# KDE Plasma: the taskbar widget and the start menu search replace the bubble.
plasma=""
if ((plasma_integration)) && [[ "${XDG_CURRENT_DESKTOP:-}" == *KDE* || -n "${KDE_SESSION_VERSION:-}" ]]; then
  plasma="${KDE_SESSION_VERSION:-6}"
  bubble=0
fi

packages=()
if [[ -z "$python" ]]; then
  packages+=(python3.11 python3.11-venv) # Ubuntu 22.04: Python 3.10 by default
elif ! "$python" -c 'import ensurepip, venv' 2>/dev/null; then
  version="$("$python" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
  packages+=("python$version-venv")
fi
packages+=(libglib2.0-bin xdg-utils)
# Administrator rights for software installation, through polkit's password dialog.
if ! command -v pkexec >/dev/null; then
  if command -v apt-cache >/dev/null && ! available pkexec; then
    packages+=(policykit-1) # Ubuntu 22.04, Debian 11
  else
    packages+=(pkexec)
  fi
fi
if [[ "$plasma" == 5 ]]; then
  packages+=(qml-module-qtwebengine) # the taskbar widget shows the assistant with it
elif [[ -n "$plasma" ]]; then
  packages+=(qml6-module-qtwebengine)
fi
if ((bubble)); then
  packages+=(python3-gi python3-gi-cairo gir1.2-gtk-3.0)
  if command -v apt-cache >/dev/null && ! available gir1.2-webkit2-4.1 && available gir1.2-webkit2-4.0; then
    packages+=(gir1.2-webkit2-4.0) # older systems
  else
    packages+=(gir1.2-webkit2-4.1)
  fi
fi

missing=()
if command -v dpkg-query >/dev/null; then
  for package in "${packages[@]}"; do
    installed "$package" || missing+=("$package")
  done
fi

if ((${#missing[@]})); then
  if ((!system_packages)); then
    warn "Paquets système absents (non installés à votre demande) : ${missing[*]}"
  elif ! command -v apt-get >/dev/null; then
    warn "Système sans apt : installez vous-même l'équivalent de : ${missing[*]}"
  else
    say "Paquets système nécessaires : ${missing[*]}"
    if ask "Les installer maintenant (sudo apt-get install) ?"; then
      sudo=()
      ((EUID == 0)) || sudo=(sudo)
      "${sudo[@]}" apt-get update -q
      "${sudo[@]}" apt-get install -y -q --no-install-recommends "${missing[@]}"
    else
      warn "Paquets non installés : certaines fonctions (bulle, Python récent) pourraient manquer."
    fi
  fi
fi

[[ -n "$python" ]] || python="$(find_python || true)"
[[ -n "$python" ]] || die "Python 3.11 ou plus récent est nécessaire (sudo apt install python3.11 python3.11-venv)."

# --- Kreafinity, in its own Python environment ------------------------------------------------

mkdir -p "$DATA_DIR"
chmod 700 "$DATA_DIR"
if [[ ! -x "$VENV/bin/python" ]]; then
  say "Création de l'environnement Python ($("$python" --version))…"
  "$python" -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade pip

# From GitHub, install the branch's current commit exactly, and remember it:
# Kreafinity then knows when an update is available.
commit=""
if [[ -z "${KREAFINITY_SOURCE:-}" ]]; then
  commit="$(latest_commit "${REPOSITORY#https://github.com/}" "$REF")"
  if [[ -n "$commit" ]]; then
    SOURCE="$REPOSITORY/archive/$commit.tar.gz"
  fi
fi

say "Installation de Kreafinity et de ses dépendances (quelques minutes)…"
"$VENV/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade "$SOURCE"
# Versions share their number between releases: when Kreafinity was already
# installed, pip keeps the old files. Make sure the new code is used.
"$VENV/bin/python" -m pip install --quiet --disable-pip-version-check --force-reinstall --no-deps "$SOURCE"
if [[ -z "${KREAFINITY_SOURCE:-}" ]]; then
  "$VENV/bin/python" -m kreafinity.update record "$REPOSITORY" "$REF" "$commit"
else
  rm -f "$DATA_DIR/install.json" # your own copy: you update it yourself
fi

mkdir -p "$BIN_DIR"
ln -sf "$VENV/bin/kreafinity" "$BIN_DIR/kreafinity"
ln -sf "$VENV/bin/kreafinity-core" "$BIN_DIR/kreafinity-core"
kreafinity=("$VENV/bin/kreafinity")
say "Commande installée : $BIN_DIR/kreafinity ($("${kreafinity[@]}" version | tr -d '\n'))"

# --- Session: automatic start, applications menu, bubble -------------------------------------

if ((bubble)); then
  "${kreafinity[@]}" setup
else
  "${kreafinity[@]}" setup --no-bubble
fi

if [[ -n "$plasma" ]]; then
  say "KDE Plasma $plasma détecté : recherche « Demander à Kreafinity » et widget de la barre des tâches"
  desktop=(desktop install --plasma "$plasma")
  if ask "Appliquer le thème Kreafinity (couleurs et fond d'écran) ?"; then
    desktop+=(--theme)
  fi
  if ask_no "Réorganiser le bureau à la façon de Windows (vos panneaux actuels seront remplacés) ?"; then
    desktop+=(--layout --yes)
  fi
  "${kreafinity[@]}" "${desktop[@]}" || warn "Intégration à Plasma incomplète : « kreafinity desktop install » plus tard."
fi

# --- Choice of the AI ----------------------------------------------------------------------------

if [[ -z "$provider" ]]; then
  if ((assume_yes)) || ! has_tty; then
    provider="later"
  else
    cat >/dev/tty <<EOF

${BOLD}Quelle intelligence artificielle voulez-vous utiliser ?${RESET}
  1) Ollama — sur cet ordinateur : gratuit, privé, hors ligne (environ 5 Go à télécharger)
  2) Ollama Cloud — les grands modèles d'Ollama en ligne, avec une clé API
  3) Claude (Anthropic) — en ligne, avec une clé API
  4) GPT (OpenAI) — en ligne, avec une clé API
  5) Mistral AI — en ligne, avec une clé API
  6) OpenRouter — des centaines de modèles avec une seule clé API
  7) Plus tard
EOF
    printf '%s?%s Votre choix [1-7] : ' "$BOLD" "$RESET" >/dev/tty
    read -r choice </dev/tty || choice=7
    case "$choice" in
      1) provider="ollama" ;;
      2) provider="ollama-cloud" ;;
      3) provider="anthropic" ;;
      4) provider="openai" ;;
      5) provider="mistral" ;;
      6) provider="openrouter" ;;
      *) provider="later" ;;
    esac
  fi
fi

case "$provider" in
  ollama)
    # A failure here must not stop the installation: Ollama can be set up later.
    if ! command -v ollama >/dev/null; then
      if ask "Installer Ollama avec son script officiel (https://ollama.com/install.sh, demande sudo) ?"; then
        ollama_script="$(mktemp)"
        if curl -fsSL -o "$ollama_script" https://ollama.com/install.sh && sh "$ollama_script"; then
          say "Ollama est installé."
        else
          warn "Échec de l'installation d'Ollama : réessayez plus tard (voir https://ollama.com)."
        fi
        rm -f "$ollama_script"
      else
        warn "Ollama non installé : voir https://ollama.com"
      fi
    fi
    if command -v ollama >/dev/null && ask "Télécharger le modèle $OLLAMA_MODEL (environ 5 Go) ?"; then
      ollama pull "$OLLAMA_MODEL" || warn "Modèle non téléchargé : « ollama pull $OLLAMA_MODEL » plus tard."
    fi
    "${kreafinity[@]}" provider use "ollama/$OLLAMA_MODEL"
    ;;
  ollama-cloud | anthropic | openai | mistral | openrouter)
    if has_tty; then
      "${kreafinity[@]}" provider key "$provider" </dev/tty || warn "Clé non enregistrée : « kreafinity provider key $provider » plus tard."
    else
      warn "Enregistrez votre clé plus tard : kreafinity provider key $provider"
    fi
    "${kreafinity[@]}" provider use "$provider"
    ;;
  later)
    say "IA à choisir plus tard : kreafinity provider use … (voir kreafinity provider list)"
    ;;
esac

# --- Done ----------------------------------------------------------------------------------------

if ((bubble)) && [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]; then
  "${kreafinity[@]}" bubble || true
fi

echo
say "${BOLD}Kreafinity est installé !${RESET}"
if [[ -n "$plasma" ]]; then
  echo "  • Le bouton ∞ de la barre des tâches, ou tapez votre question dans le menu démarrer"
elif ((bubble)); then
  echo "  • La bulle en bas à droite de l'écran : un clic pour poser une question"
fi
echo "  • L'interface complète : « Kreafinity » dans le menu des applications, ou : kreafinity ui"
echo "  • Dans un terminal : kreafinity chat"
echo "  • Pour désinstaller : kreafinity uninstall (ajoutez --purge pour effacer aussi vos données)"
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
  warn "$BIN_DIR n'est pas encore dans votre PATH : ouvrez une nouvelle session (ou un nouveau terminal)."
fi
