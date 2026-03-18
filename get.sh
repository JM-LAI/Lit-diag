#!/usr/bin/env bash
# Lit-Diag installer -- one command, zero hassle.
#
#   curl -fsSL https://raw.githubusercontent.com/JM-LAI/Lit-diag/main/get.sh | bash
#
# That's it. Run it again to update.
set -euo pipefail

REPO="https://github.com/JM-LAI/Lit-diag.git"
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
DIM='\033[2m'
BOLD='\033[1m'
NC='\033[0m'

info()  { echo -e "${GREEN}▸${NC} $*"; }
warn()  { echo -e "${YELLOW}▸${NC} $*"; }
die()   { echo -e "${RED}▸${NC} $*" >&2; exit 1; }

# where to put things -- /opt if we can write there, ~ otherwise
if [ -w "/opt" ] || [ "$(id -u)" -eq 0 ]; then
    INSTALL_DIR="/opt/lit-diag"
    BIN_DIR="/usr/local/bin"
else
    INSTALL_DIR="$HOME/.lit-diag"
    BIN_DIR="$HOME/.local/bin"
    mkdir -p "$BIN_DIR"
fi

VENV_DIR="$INSTALL_DIR/venv"

# ── preflight ──────────────────────────────────────────────
command -v python3 &>/dev/null || die "python3 not found. Install Python 3.9+ first."

PY_VER=$(python3 -c "import sys; v=sys.version_info; print(f'{v.major}.{v.minor}')")
PY_MAJOR=${PY_VER%%.*}
PY_MINOR=${PY_VER#*.}
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 9 ]; }; then
    die "Python 3.9+ required (found $PY_VER)."
fi

# auto-install missing system packages if we're root
if ! python3 -m venv --help &>/dev/null 2>&1; then
    if [ "$(id -u)" -eq 0 ]; then
        info "Installing python3-venv..."
        apt-get update -qq && apt-get install -y -qq "python${PY_VER}-venv" >/dev/null 2>&1 \
            || apt-get install -y -qq python3-venv >/dev/null 2>&1 \
            || die "Could not install python3-venv. Install manually: apt install python3-venv"
    else
        die "python3-venv not available. Run with sudo or: sudo apt install python3-venv"
    fi
fi

if ! command -v git &>/dev/null; then
    if [ "$(id -u)" -eq 0 ]; then
        info "Installing git..."
        apt-get update -qq && apt-get install -y -qq git >/dev/null 2>&1 \
            || die "Could not install git. Install manually: apt install git"
    else
        die "git not found. Run with sudo or: sudo apt install git"
    fi
fi

# ── install / update ───────────────────────────────────────
echo ""
echo -e "${BOLD}  Lit-Diag Installer${NC}"
echo -e "  ${DIM}─────────────────────────────${NC}"
echo ""

if [ -x "$VENV_DIR/bin/python" ]; then
    info "Updating existing installation..."
    ACTION="update"
else
    # clean up any broken previous attempt
    [ -d "$INSTALL_DIR" ] && rm -rf "$INSTALL_DIR"
    info "Installing to ${DIM}$INSTALL_DIR${NC}"
    ACTION="install"
    mkdir -p "$INSTALL_DIR"
    python3 -m venv "$VENV_DIR"
fi

# activate and install/upgrade from the repo
"$VENV_DIR/bin/pip" install --upgrade --quiet pip 2>/dev/null || true
"$VENV_DIR/bin/pip" install --upgrade --quiet "git+${REPO}" 2>&1 | grep -v "^$" || true

# verify the package landed
if ! "$VENV_DIR/bin/python" -c "import lit_diag" 2>/dev/null; then
    die "Installation failed -- lit_diag module not found in venv."
fi

VERSION=$("$VENV_DIR/bin/python" -c "from lit_diag import __version__; print(__version__)" 2>/dev/null || echo "unknown")

# ── create wrapper binary ─────────────────────────────────
# this wrapper activates the venv so sudo/root/any-user just works
WRAPPER="$BIN_DIR/lit-diag"
cat > "$WRAPPER" << WRAPPER_EOF
#!/usr/bin/env bash
exec "$VENV_DIR/bin/python" -m lit_diag.cli "\$@"
WRAPPER_EOF
chmod +x "$WRAPPER"

# ── verify ─────────────────────────────────────────────────
echo ""
if [ "$ACTION" = "install" ]; then
    info "Installed ${BOLD}lit-diag v${VERSION}${NC}"
else
    info "Updated to ${BOLD}lit-diag v${VERSION}${NC}"
fi
info "Binary: ${DIM}$WRAPPER${NC}"

# check if bin dir is on PATH
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    echo ""
    warn "$BIN_DIR is not on your PATH."
    warn "Add this to your shell profile:"
    echo -e "    ${DIM}export PATH=\"$BIN_DIR:\$PATH\"${NC}"
    echo ""
    warn "Or just run it directly:"
    echo -e "    ${DIM}$WRAPPER${NC}"
else
    echo ""
    info "Run it:"
    echo -e "    ${DIM}lit-diag${NC}                               ${DIM}# interactive menu${NC}"
    echo -e "    ${DIM}lit-diag run --all${NC}                     ${DIM}# full diagnostic${NC}"
    echo -e "    ${DIM}lit-diag run --all --json -o report.json${NC}"
    echo -e "    ${DIM}sudo lit-diag run --all${NC}                ${DIM}# with root checks${NC}"
fi
echo ""
