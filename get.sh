#!/usr/bin/env bash
# Lit-Diag installer -- one command, zero hassle.
#
#   curl -fsSL https://raw.githubusercontent.com/JM-LAI/Lit-diag/main/get.sh | bash
#
# That's it. Run it again to update.
# Works on broken apt, missing python3-venv, held packages -- doesn't care.
set -euo pipefail

REPO="https://github.com/JM-LAI/Lit-diag.git"
GET_PIP_URL="https://bootstrap.pypa.io/get-pip.py"
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
DIM='\033[2m'
BOLD='\033[1m'
NC='\033[0m'

info()  { echo -e "${GREEN}▸${NC} $*"; }
warn()  { echo -e "${YELLOW}▸${NC} $*"; }
die()   { echo -e "${RED}▸${NC} $*" >&2; exit 1; }

# ── where to put things ───────────────────────────────────
if [ -w "/opt" ] || [ "$(id -u)" -eq 0 ]; then
    INSTALL_DIR="/opt/lit-diag"
    BIN_DIR="/usr/local/bin"
else
    INSTALL_DIR="$HOME/.lit-diag"
    BIN_DIR="$HOME/.local/bin"
    mkdir -p "$BIN_DIR"
fi

VENV_DIR="$INSTALL_DIR/venv"
VENV_PY="$VENV_DIR/bin/python"
VENV_PIP="$VENV_DIR/bin/pip"

# ── preflight ──────────────────────────────────────────────
command -v python3 &>/dev/null || die "python3 not found. Install Python 3.9+ first."
command -v curl &>/dev/null    || die "curl not found."

PY_VER=$(python3 -c "import sys; v=sys.version_info; print(f'{v.major}.{v.minor}')")
PY_MAJOR=${PY_VER%%.*}
PY_MINOR=${PY_VER#*.}
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 9 ]; }; then
    die "Python 3.9+ required (found $PY_VER)."
fi

# git is needed to pip install from a repo
if ! command -v git &>/dev/null; then
    if [ "$(id -u)" -eq 0 ]; then
        info "Installing git..."
        apt-get update -qq >/dev/null 2>&1 && apt-get install -y -qq git >/dev/null 2>&1 \
            || yum install -y -q git >/dev/null 2>&1 \
            || die "Could not install git automatically."
    else
        die "git not found. Run with sudo or install git first."
    fi
fi

# ── banner ─────────────────────────────────────────────────
echo ""
echo -e "${BOLD}  Lit-Diag Installer${NC}"
echo -e "  ${DIM}─────────────────────────────${NC}"
echo ""

# ── create or reuse venv ───────────────────────────────────
if [ -x "$VENV_PY" ] && [ -x "$VENV_PIP" ]; then
    info "Updating existing installation..."
    ACTION="update"
else
    # wipe any broken leftovers
    [ -d "$INSTALL_DIR" ] && rm -rf "$INSTALL_DIR"
    ACTION="install"
    info "Installing to ${DIM}$INSTALL_DIR${NC}"
    mkdir -p "$INSTALL_DIR"

    # try normal venv first (suppress all noise)
    if python3 -m venv "$VENV_DIR" >/dev/null 2>&1; then
        true
    elif [ "$(id -u)" -eq 0 ]; then
        # venv failed (usually missing ensurepip / python3-venv)
        info "Setting up Python environment..."
        rm -rf "$VENV_DIR"

        # figure out the full python package name (e.g. python3.10)
        PY_PKG="python${PY_VER}"

        # GPU nodes often hold every package to prevent CUDA breakage.
        # temporarily unhold python so we can install python3.X-venv.
        HELD_PKGS=()
        for pkg in "${PY_PKG}" "${PY_PKG}-minimal" "${PY_PKG}-dev" \
                   "lib${PY_PKG}-minimal" "lib${PY_PKG}-stdlib" \
                   "lib${PY_PKG}-dev" "lib${PY_PKG}" \
                   "python3" "python3-minimal" "python3-dev" \
                   "libpython3-dev" "libpython3-stdlib"; do
            if apt-mark showhold 2>/dev/null | grep -qx "$pkg"; then
                HELD_PKGS+=("$pkg")
            fi
        done

        if [ ${#HELD_PKGS[@]} -gt 0 ]; then
            info "Temporarily unholding ${#HELD_PKGS[@]} Python packages..."
            apt-mark unhold "${HELD_PKGS[@]}" >/dev/null 2>&1 || true
        fi

        # now try to install python3.X-venv
        apt-get update -qq >/dev/null 2>&1 || true
        apt-get --fix-broken install -y -qq >/dev/null 2>&1 || true
        dpkg --configure -a >/dev/null 2>&1 || true
        apt-get install -y -qq "${PY_PKG}-venv" >/dev/null 2>&1 \
            || apt-get install -y -qq python3-venv >/dev/null 2>&1 \
            || true

        # re-hold everything we unholded
        if [ ${#HELD_PKGS[@]} -gt 0 ]; then
            apt-mark hold "${HELD_PKGS[@]}" >/dev/null 2>&1 || true
        fi

        if python3 -m venv "$VENV_DIR" >/dev/null 2>&1; then
            true
        else
            # apt truly can't help -- bootstrap without it
            info "Bootstrapping pip manually..."
            rm -rf "$VENV_DIR"
            python3 -m venv --without-pip "$VENV_DIR" >/dev/null 2>&1 \
                || die "Cannot create Python environment. Is python3 installed correctly?"
            curl -fsSL "$GET_PIP_URL" -o /tmp/_get_pip.py
            "$VENV_PY" /tmp/_get_pip.py --quiet >/dev/null 2>&1 || true
            rm -f /tmp/_get_pip.py
        fi
    else
        # not root, can't fix apt -- go straight to manual bootstrap
        info "Bootstrapping pip manually..."
        rm -rf "$VENV_DIR"
        python3 -m venv --without-pip "$VENV_DIR" >/dev/null 2>&1 \
            || die "Cannot create Python environment. Try running with sudo."
        curl -fsSL "$GET_PIP_URL" -o /tmp/_get_pip.py
        "$VENV_PY" /tmp/_get_pip.py --quiet >/dev/null 2>&1 || true
        rm -f /tmp/_get_pip.py
    fi
fi

# make sure pip exists in the venv
if [ ! -x "$VENV_PIP" ]; then
    info "Bootstrapping pip..."
    curl -fsSL "$GET_PIP_URL" -o /tmp/_get_pip.py
    "$VENV_PY" /tmp/_get_pip.py --quiet >/dev/null 2>&1 || true
    rm -f /tmp/_get_pip.py
fi

[ -x "$VENV_PIP" ] || die "Could not set up pip. Check your Python installation."

# ── install / upgrade lit-diag from the repo ──────────────
info "Installing lit-diag from GitHub..."
"$VENV_PIP" install --upgrade --quiet pip >/dev/null 2>&1 || true
"$VENV_PIP" install --upgrade --quiet "git+${REPO}" >/dev/null 2>&1 || true

# verify
if ! "$VENV_PY" -c "import lit_diag" 2>/dev/null; then
    die "Installation failed -- lit_diag module not found."
fi

VERSION=$("$VENV_PY" -c "from lit_diag import __version__; print(__version__)" 2>/dev/null || echo "unknown")

# ── create wrapper binary ─────────────────────────────────
WRAPPER="$BIN_DIR/lit-diag"
cat > "$WRAPPER" << WRAPPER_EOF
#!/usr/bin/env bash
exec "$VENV_PY" -m lit_diag.cli "\$@"
WRAPPER_EOF
chmod +x "$WRAPPER"

# ── done ───────────────────────────────────────────────────
echo ""
if [ "$ACTION" = "install" ]; then
    info "Installed ${BOLD}lit-diag v${VERSION}${NC}"
else
    info "Updated to ${BOLD}lit-diag v${VERSION}${NC}"
fi
info "Binary: ${DIM}$WRAPPER${NC}"

if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    echo ""
    warn "$BIN_DIR is not on your PATH."
    warn "Run directly: ${DIM}$WRAPPER${NC}"
else
    echo ""
    info "Run it:"
    echo -e "    ${DIM}lit-diag${NC}                               ${DIM}# interactive menu${NC}"
    echo -e "    ${DIM}lit-diag run --all${NC}                     ${DIM}# full diagnostic${NC}"
    echo -e "    ${DIM}lit-diag run --all --json -o report.json${NC}"
    echo -e "    ${DIM}sudo lit-diag run --all${NC}                ${DIM}# with root checks${NC}"
fi
echo ""
