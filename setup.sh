#!/usr/bin/env bash
# Gjallarhorn setup script.
# Installs system packages, creates the Python venv, installs requirements,
# grants the capabilities the WiFi scanner needs, makes sure the user is in
# the bluetooth group, enables gpsd, and verifies the data dirs are writable.
#
# Re-running is safe — every step is idempotent.
#
#   ./setup.sh                 # normal run; will sudo where needed
#   ./setup.sh --no-sudo       # skip every step that requires root
#   ./setup.sh --dev           # also install pytest/ruff for local development
#   ./setup.sh --with-hackrf   # also install hackrf-tools + build btle_rx
#                              # from JiaoXianjun/BTLE into /usr/local/bin
#                              # so the HackRF BLE scanner works

set -euo pipefail

NO_SUDO=0
DEV=0
WITH_HACKRF=0
for arg in "$@"; do
    case "$arg" in
        --no-sudo)     NO_SUDO=1 ;;
        --dev)         DEV=1 ;;
        --with-hackrf) WITH_HACKRF=1 ;;
        -h|--help)
            sed -n '2,16p' "$0"
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

# ---- 5b. HackRF BLE scanner (optional, --with-hackrf) -----------------------
#
# The HackRF BLE scanner wraps `btle_rx` from JiaoXianjun/BTLE as an SDR-backed
# BLE advertising sniffer. Strictly optional — the bleak-based BLE scanner
# covers ~90% of what most operators need. Pass --with-hackrf to install the
# build deps + hackrf user tools and build btle_rx into /usr/local/bin. Without
# the flag, we just print a hint if a HackRF is detected.
#
# The runtime detection in services/hackrf_ble_scanner.py probes a list of
# standard install locations (incl. /usr/local/bin) directly, so PATH doesn't
# need /usr/local/bin to be set in the gjallarhorn process's environment —
# the binary will be found regardless of how the app is launched.
BTLE_SRC_DIR="$SCRIPT_DIR/vendor/BTLE"

# Returns 0 if libhackrf headers are already discoverable. Used to skip
# the apt install of hackrf / libhackrf-dev when the operator has
# libhackrf installed from upstream source (or a PPA) — Ubuntu's
# packaged versions often conflict with anything newer than the LTS
# baseline.
_libhackrf_headers_present() {
    [[ -f /usr/include/libhackrf/hackrf.h \
       || -f /usr/local/include/libhackrf/hackrf.h ]]
}

_libfftw_present() {
    [[ -f /usr/include/fftw3.h \
       || -f /usr/local/include/fftw3.h ]]
}

install_hackrf_stack() {
    # Build only the package list we actually need so a pre-existing
    # libhackrf install (newer than Ubuntu's, common via PPA / source
    # build) doesn't trip apt into a 'held broken packages' error
    # trying to downgrade libhackrf0.
    local apt_pkgs=()
    have cmake || apt_pkgs+=(cmake)
    have make  || apt_pkgs+=(build-essential)
    have git   || apt_pkgs+=(git)
    _libfftw_present || apt_pkgs+=(libfftw3-dev)
    if ! have hackrf_info; then
        apt_pkgs+=(hackrf)
    fi
    if ! _libhackrf_headers_present; then
        apt_pkgs+=(libhackrf-dev)
    fi

    if [[ ${#apt_pkgs[@]} -eq 0 ]]; then
        log "HackRF build deps already satisfied — skipping apt"
    elif have apt-get; then
        log "installing HackRF build deps: ${apt_pkgs[*]}"
        if ! run_sudo apt-get install -y "${apt_pkgs[@]}"; then
            warn ""
            warn "apt install failed — likely a libhackrf version conflict."
            warn "  Your system probably has libhackrf0 from a PPA or a source"
            warn "  build that's newer than the version Ubuntu's 'hackrf' and"
            warn "  'libhackrf-dev' packages pin. Two ways out:"
            warn ""
            warn "  A) Keep your newer libhackrf, install headers from upstream:"
            warn "       git clone https://github.com/greatscottgadgets/hackrf"
            warn "       cd hackrf/host && mkdir build && cd build"
            warn "       cmake .. && make && sudo make install"
            warn ""
            warn "  B) Downgrade libhackrf0 to Ubuntu's pinned version:"
            warn "       sudo apt install --allow-downgrades \\"
            warn "         libhackrf0=2023.01.1-9build1 hackrf libhackrf-dev"
            warn ""
            warn "  Once libhackrf headers are present, re-run:"
            warn "    ./setup.sh --with-hackrf"
            warn ""
            return 1
        fi
    elif have dnf; then
        run_sudo dnf install -y hackrf-devel fftw-devel cmake gcc-c++ git || \
            { warn "dnf install failed — install manually"; return 1; }
    elif have pacman; then
        run_sudo pacman -S --needed --noconfirm hackrf fftw cmake base-devel git || \
            { warn "pacman install failed — install manually"; return 1; }
    else
        warn "no supported package manager — install hackrf, libhackrf-dev,"
        warn "  libfftw3-dev, cmake, build-essential, and git manually."
        return 1
    fi

    # Final pre-build sanity check: headers must be present or cmake
    # will bomb with a less helpful error. Bail with guidance.
    if ! _libhackrf_headers_present; then
        warn "libhackrf headers still not found after install attempt."
        warn "  Cannot build btle_rx without hackrf.h. See guidance above."
        return 1
    fi
    if ! _libfftw_present; then
        warn "libfftw3 headers still not found after install attempt."
        warn "  Cannot build btle_rx without fftw3.h. See guidance above."
        return 1
    fi

    # plugdev group so libusb can talk to the dongle without root.
    if getent group plugdev >/dev/null 2>&1; then
        if id -nG "$TARGET_USER" | tr ' ' '\n' | grep -qx plugdev; then
            log "$TARGET_USER already in plugdev group"
        else
            log "adding $TARGET_USER to plugdev group (log out/in to take effect)"
            run_sudo usermod -aG plugdev "$TARGET_USER" || \
                warn "could not add $TARGET_USER to plugdev group"
        fi
    fi

    # Clone JiaoXianjun/BTLE into ./vendor so the build stays inside the
    # project tree and re-runs are stateless (we wipe + rebuild rather than
    # carrying stale build state across runs).
    mkdir -p "$(dirname "$BTLE_SRC_DIR")"
    if [[ -d "$BTLE_SRC_DIR/.git" ]]; then
        log "updating JiaoXianjun/BTLE clone at $BTLE_SRC_DIR"
        git -C "$BTLE_SRC_DIR" fetch --depth 1 origin >/dev/null 2>&1 || \
            warn "git fetch failed — using existing clone"
        git -C "$BTLE_SRC_DIR" reset --hard origin/HEAD >/dev/null 2>&1 || true
    else
        log "cloning JiaoXianjun/BTLE → $BTLE_SRC_DIR"
        rm -rf "$BTLE_SRC_DIR"
        git clone --depth 1 https://github.com/JiaoXianjun/BTLE.git "$BTLE_SRC_DIR"
    fi

    # cmake + make + install. Wipe and recreate the build dir so a partial
    # prior run doesn't trip cmake's cache.
    local build_dir="$BTLE_SRC_DIR/host/build"
    log "building btle_rx (cmake + make in $build_dir)"
    rm -rf "$build_dir"
    mkdir -p "$build_dir"
    ( cd "$build_dir" && cmake .. && make -j"$(nproc 2>/dev/null || echo 2)" )

    # Install to /usr/local/bin where the runtime resolver looks first.
    log "installing btle_rx to /usr/local/bin (needs sudo)"
    ( cd "$build_dir" && run_sudo make install )

    if have btle_rx || [[ -x /usr/local/bin/btle_rx ]]; then
        log "btle_rx installed at: $(command -v btle_rx 2>/dev/null || echo /usr/local/bin/btle_rx)"
    else
        warn "btle_rx still not found after install — check the make output above"
    fi
}

if [[ "$WITH_HACKRF" -eq 1 ]]; then
    # Swallow install failure so the rest of setup.sh still completes —
    # the operator gets a clear diagnostic from install_hackrf_stack
    # explaining what to do (libhackrf version conflict, etc.) and the
    # core Gjallarhorn install isn't blocked on an optional component.
    install_hackrf_stack || warn "HackRF setup skipped — see messages above"
elif have hackrf_info; then
    log "hackrf_info detected — HackRF BLE scanner is available"
    if ! have btle_rx && [[ ! -x /usr/local/bin/btle_rx ]]; then
        warn "btle_rx not installed — re-run with --with-hackrf to build it"
        warn "  (or follow the manual steps in JiaoXianjun/BTLE's README)"
    fi
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
if [[ "$WITH_HACKRF" -eq 1 ]] \
   && ! id -nG "$TARGET_USER" | tr ' ' '\n' | grep -qx plugdev; then
    warn "you were just added to the plugdev group — log out and back in"
    warn "(or run: newgrp plugdev) before the HackRF will be accessible."
fi
