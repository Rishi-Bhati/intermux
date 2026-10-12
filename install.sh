#!/usr/bin/env bash
# Install InterMux system-wide on Linux. Run as root: sudo ./install.sh
#
# If dist/intermux/intermux exists in the repo it is installed as-is.
# Otherwise the PyInstaller build runs as the invoking user ($SUDO_USER)
# in a temporary virtualenv and temporary build directory, never as root
# and never inside the repository. Re-running this script is safe.
set -euo pipefail

readonly PYINSTALLER_VERSION="6.11.1"
readonly INSTALL_LIB="/usr/lib/intermux"
readonly INSTALL_BIN="/usr/bin/intermux"
readonly LEGACY_BIN="/usr/local/bin/intermux"
readonly POLKIT_DIR="/usr/share/polkit-1/actions"
readonly APPS_DIR="/usr/share/applications"
readonly ICON_DIR="/usr/share/icons/hicolor/scalable/apps"
readonly META_DIR="/usr/share/metainfo"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
BUILD_TMP=""

info() { printf '[+] %s\n' "$*"; }
ok()   { printf '[✓] %s\n' "$*"; }
warn() { printf '[!] %s\n' "$*" >&2; }
die()  { printf '[X] %s\n' "$*" >&2; exit 1; }

cleanup() {
    if [ -n "$BUILD_TMP" ] && [ -d "$BUILD_TMP" ]; then
        rm -rf -- "$BUILD_TMP"
    fi
}
trap cleanup EXIT

install_hint() {
    local distro
    distro="$(sed -n 's/^ID=//p' /etc/os-release 2>/dev/null | tr -d '"')"
    case "$distro" in
        arch|manjaro|endeavouros)
            echo "sudo pacman -S --needed iproute2 iptables procps-ng util-linux polkit" ;;
        debian|ubuntu|linuxmint|pop)
            echo "sudo apt install iproute2 iptables procps util-linux policykit-1" ;;
        fedora|rhel|centos|rocky|almalinux)
            echo "sudo dnf install iproute iptables procps-ng util-linux polkit" ;;
        *)
            echo "install iproute2, iptables, procps, util-linux and polkit (package names vary by distro)" ;;
    esac
}

check_tools() {
    local tool
    local missing=()
    for tool in ip iptables runuser pkexec; do
        command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
    done
    if [ "${#missing[@]}" -eq 0 ]; then
        ok "Runtime tools found"
    else
        warn "Missing runtime tools: ${missing[*]}"
        warn "Install hint: $(install_hint)"
    fi
    if ! command -v pgrep >/dev/null 2>&1; then
        warn "pgrep not found (optional, not required by InterMux)"
    fi
}

require_sources() {
    local f
    for f in \
        packaging/linux/org.intermux.policy \
        packaging/linux/intermux.desktop \
        packaging/linux/intermux.svg \
        packaging/linux/intermux.metainfo.xml \
        intermux_linux.spec \
        intermux/__main__.py; do
        [ -f "$SCRIPT_DIR/$f" ] || die "Missing $f next to install.sh"
    done
}

# Builds the onedir bundle as $SUDO_USER. Sets BUILD_TMP and BUILD_DIST.
build_onedir() {
    local user="$SUDO_USER"
    local py venv

    py="$(command -v python3 || true)"
    [ -n "$py" ] || die "python3 not found. Install Python 3 and the venv module (python3-venv on Debian/Ubuntu)."

    info "Building InterMux as user '$user' in a temporary directory"
    BUILD_TMP="$(sudo -u "$user" -- mktemp -d)"
    venv="$BUILD_TMP/venv"

    sudo -u "$user" -- "$py" -m venv "$venv" \
        || die "Could not create a virtualenv (is the venv module installed?)"
    # Supply chain note: the version is pinned but hashes are not verified.
    # Follow-up: use a hash file with `pip install --require-hashes -r ...`.
    sudo -u "$user" -- "$venv/bin/pip" install --quiet --disable-pip-version-check \
        "pyinstaller==$PYINSTALLER_VERSION" \
        || die "Could not install pyinstaller==$PYINSTALLER_VERSION"

    (
        cd -- "$SCRIPT_DIR"
        sudo -u "$user" -- "$venv/bin/pyinstaller" intermux_linux.spec \
            --noconfirm \
            --distpath "$BUILD_TMP/dist" \
            --workpath "$BUILD_TMP/build"
    ) || die "PyInstaller build failed"

    BUILD_DIST="$BUILD_TMP/dist/intermux"
    [ -x "$BUILD_DIST/intermux" ] || die "PyInstaller did not produce $BUILD_DIST/intermux"
    ok "Built $BUILD_DIST"
}

# Privilege note: the polkit action org.intermux.run authorises running the
# installed binary ($INSTALL_LIB/intermux) through pkexec, not only its
# `helper` subcommand. InterMux calls it as `intermux helper`. The binary
# refusing every command other than `helper` when run as root is PLANNED
# (intermux/__main__.py) and is not implemented yet.
#
# The root-executed binary must be owned by root and not writable by others.
verify_install_lib() {
    local target="$INSTALL_LIB/intermux"
    local info_line owner mode

    info_line="$(stat -c '%u %a' -- "$target")" || die "Could not stat $target"
    owner="${info_line%% *}"
    mode="${info_line##* }"
    [ "$owner" = "0" ] || die "$target must be owned by root (uid 0), found uid $owner"
    if (( (8#$mode) & 022 )); then
        die "$target is group- or world-writable (mode $mode)"
    fi
}

install_files() {
    local src="$1"

    info "Installing bundle to $INSTALL_LIB"
    rm -rf -- "$INSTALL_LIB"
    install -d -m 0755 "$INSTALL_LIB"
    cp -r --no-preserve=ownership -- "$src/." "$INSTALL_LIB/"
    chown -R root:root -- "$INSTALL_LIB"
    chmod -R a+rX,go-w -- "$INSTALL_LIB"
    find "$INSTALL_LIB" -type d -exec chmod 0755 -- {} +
    chmod 0755 "$INSTALL_LIB/intermux"
    verify_install_lib
    ok "Bundle owned by root and not writable by other users"

    if [ -f "$LEGACY_BIN" ] && [ ! -L "$LEGACY_BIN" ]; then
        info "Removing old copy at $LEGACY_BIN"
        rm -f -- "$LEGACY_BIN"
    fi

    if [ -e "$INSTALL_BIN" ] && [ ! -L "$INSTALL_BIN" ]; then
        die "$INSTALL_BIN exists and is not a symlink; refusing to overwrite it"
    fi
    ln -sfn -- "$INSTALL_LIB/intermux" "$INSTALL_BIN"
    ok "Command: $INSTALL_BIN"

    install -Dm644 "$SCRIPT_DIR/packaging/linux/org.intermux.policy" \
        "$POLKIT_DIR/org.intermux.policy"
    install -Dm644 "$SCRIPT_DIR/packaging/linux/intermux.desktop" \
        "$APPS_DIR/intermux.desktop"
    install -Dm644 "$SCRIPT_DIR/packaging/linux/intermux.svg" \
        "$ICON_DIR/intermux.svg"
    install -Dm644 "$SCRIPT_DIR/packaging/linux/intermux.metainfo.xml" \
        "$META_DIR/org.intermux.InterMux.metainfo.xml"
    ok "Installed polkit policy, desktop entry, icon and metainfo"

    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database -q "$APPS_DIR" || warn "update-desktop-database failed"
    fi
    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
        gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || warn "gtk-update-icon-cache failed"
    fi
}

main() {
    [ "$(uname -s)" = "Linux" ] || die "This installer only supports Linux."
    [ "$(id -u)" -eq 0 ] || die "Please run as root (sudo ./install.sh)"

    info "Installing InterMux"
    require_sources
    check_tools

    local dist_dir
    if [ -x "$SCRIPT_DIR/dist/intermux/intermux" ]; then
        dist_dir="$SCRIPT_DIR/dist/intermux"
        info "Using prebuilt bundle in $dist_dir"
    else
        if [ -z "${SUDO_USER:-}" ] || [ "$SUDO_USER" = "root" ]; then
            die "No prebuilt dist/intermux/ found and SUDO_USER is not set. Run 'sudo ./install.sh' from a normal user account."
        fi
        build_onedir
        dist_dir="$BUILD_DIST"
    fi

    install_files "$dist_dir"

    ok "InterMux installed. Launch it from the application menu or run 'intermux'."
}

main "$@"
