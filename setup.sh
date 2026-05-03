#!/usr/bin/env bash
# Gjallarhorn setup script.
# Installs system packages, creates the Python venv, installs requirements,
# grants the capabilities the WiFi scanner needs, makes sure the user is in
# the bluetooth group, enables gpsd, and verifies the data dirs are writable.
#
# Re-running is safe — every step is idempotent.
#
#   ./setup.sh           # normal run; will sudo where needed
#   ./setup.sh --no-sudo # skip every step that requires root
#   ./setup.sh --dev     # also install pytest/ruff for local development

set -euo pipefail

NO_SUDO=0
DEV=0
for arg in "$@"; do
    case "$arg" in
        --no-sudo) NO_SUDO=1 ;;
        --dev)     DEV=1 ;;
        -h|--help)
            sed -n '2,12p' "$0"
            exit 0 ;;
        *) echo "unknown flag: $arg" >&2; exit 2 ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ---- helpers ----------------------------------------------------------------

log()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!! \033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx \033[0m %s\n' "$*" >&2; exit 1; }

run_sudo() {
    if [[ "$NO_SUDO" -eq 1 ]]; then
        warn "skipping (--no-sudo): $*"
        return 0
    fi
    if [[ $EUID -eq 0 ]]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo "$@"
    else
        die "this step needs root and sudo is not installed: $*"
    fi
}

have() { command -v "$1" >/dev/null 2>&1; }

# ---- 0. sanity --------------------------------------------------------------

if [[ "$(uname -s)" != "Linux" ]]; then
    warn "Gjallarhorn's WiFi/Bluetooth scanners only work on Linux."
    warn "The web UI will still run, but expect empty scan results on $(uname -s)."
fi

# ---- 1. system packages -----------------------------------------------------

SYS_PKGS=(gpsd gpsd-clients iw bluez python3 python3-venv python3-pip libcap2-bin)

if have apt-get; then
    log "installing system packages: ${SYS_PKGS[*]}"
    run_sudo apt-get update
    run_sudo apt-get install -y "${SYS_PKGS[@]}"
elif have dnf; then
    log "installing system packages via dnf"
    # Fedora package names differ slightly.
    run_sudo dnf install -y gpsd gpsd-clients iw bluez python3 python3-pip libcap
elif have pacman; then
    log "installing system packages via pacman"
    run_sudo pacman -S --needed --noconfirm gpsd iw bluez bluez-utils python python-pip libcap
else
    warn "no supported package manager found; install these manually: ${SYS_PKGS[*]}"
fi

# ---- 2. python venv ---------------------------------------------------------

if [[ ! -d .venv ]]; then
    log "creating .venv"
    python3 -m venv .venv
else
    log ".venv already exists"
fi

# shellcheck source=/dev/null
source .venv/bin/activate

log "upgrading pip"
python -m pip install --upgrade pip wheel >/dev/null

log "installing requirements.txt"
pip install -r requirements.txt

if [[ "$DEV" -eq 1 ]]; then
    log "installing dev extras (pytest, ruff)"
    pip install pytest ruff
fi

# ---- 3. capabilities for WiFi scan / probe capture --------------------------
# Linux file caps don't propagate from a launcher (python) to its subprocess
# (iw, tshark) unless the *child* binary also has matching file inheritable
# bits or ambient caps are set. So setcap goes on the privileged tools
# themselves, which is what actually makes `iw scan` and probe capture work
# without root.

PY_BIN="$(readlink -f .venv/bin/python3)"
if have setcap; then
    grant_cap() {
        local what="$1" path="$2"
        if [[ -z "$path" ]]; then
            warn "$what not found in PATH; skipping (install it if you need this feature)"
            return
        fi
        local resolved
        resolved="$(readlink -f "$path")"
        log "granting cap_net_admin,cap_net_raw to $what ($resolved)"
        run_sudo setcap cap_net_admin,cap_net_raw+eip "$resolved" || \
            warn "setcap failed for $resolved — you'll need to run gjallarhorn.py as root"
    }
    grant_cap iw     "$(command -v iw     || true)"
    grant_cap tshark "$(command -v tshark || true)"
else
    warn "setcap not found (libcap2-bin / libcap missing); skipping capability grant"
fi

# ---- 4. bluetooth group -----------------------------------------------------

TARGET_USER="${SUDO_USER:-$USER}"
if getent group bluetooth >/dev/null 2>&1; then
    if id -nG "$TARGET_USER" | tr ' ' '\n' | grep -qx bluetooth; then
        log "$TARGET_USER already in bluetooth group"
    else
        log "adding $TARGET_USER to bluetooth group (log out/in to take effect)"
        run_sudo usermod -aG bluetooth "$TARGET_USER" || \
            warn "could not add $TARGET_USER to bluetooth group"
    fi
fi

# ---- 5. gpsd service --------------------------------------------------------

if have systemctl; then
    log "enabling and starting gpsd"
    run_sudo systemctl enable --now gpsd.socket 2>/dev/null || true
    run_sudo systemctl enable --now gpsd        2>/dev/null || true
fi

# ---- 6. data directories ----------------------------------------------------

log "ensuring tile-cache and oui-cache exist and are writable"
mkdir -p tile-cache oui-cache
chmod -R u+rwX tile-cache oui-cache

# DB file: created on first run by aiosqlite, but make sure parent is writable.
if [[ -e gjallarhorn.db ]]; then
    chmod u+rw gjallarhorn.db
fi

# ---- 7. summary -------------------------------------------------------------

cat <<EOF

$(log "setup complete")

  python:   $PY_BIN
  venv:     $SCRIPT_DIR/.venv

  to run:
    source .venv/bin/activate
    python gjallarhorn.py

  UI:       http://0.0.0.0:5003

EOF

if ! id -nG "$TARGET_USER" | tr ' ' '\n' | grep -qx bluetooth; then
    warn "you were just added to the bluetooth group — log out and back in"
    warn "(or run: newgrp bluetooth) before starting the app."
fi
