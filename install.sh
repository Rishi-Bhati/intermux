#!/usr/bin/env bash
# Install InterMux system-wide on Linux. Run as root: sudo ./install.sh
#
# Usage: sudo ./install.sh [--allow-local-bundle] [--bundle-sha256 HEX]
#
# The installed bundle is executed as root by polkit, so its origin matters:
#  - If dist/intermux/intermux exists next to this script it is installed only
#    when the whole bundle is root-owned and not group/world-writable (for
#    example a release tarball unpacked with sudo), or when you pass
#    --allow-local-bundle after reviewing the printed checksum. A checksum
#    from a trusted source can be enforced with --bundle-sha256 HEX, or by a
#    dist/intermux.sha256 file (see bundle_checksum below).
#  - Otherwise the PyInstaller build runs as the invoking user ($SUDO_USER)
#    in a temporary virtualenv and temporary build directory, never as root
#    and never inside the repository. The result is copied to a root-owned
#    staging directory before it is installed.
# Re-running this script is safe.
set -euo pipefail

readonly PYINSTALLER_VERSION="6.20.0"
readonly INSTALL_LIB="/usr/lib/intermux"
readonly INSTALL_BIN="/usr/bin/intermux"
readonly LEGACY_BIN="/usr/local/bin/intermux"
readonly POLKIT_DIR="/usr/share/polkit-1/actions"
readonly WATCH_UNIT="/usr/lib/systemd/system/intermux-watch.service"
readonly APPS_DIR="/usr/share/applications"
readonly ICON_DIR="/usr/share/icons/hicolor/scalable/apps"
readonly META_DIR="/usr/share/metainfo"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
BUILD_TMP=""
STAGE_TMP=""
ALLOW_LOCAL_BUNDLE=0
EXPECT_SHA256=""
WATCHER_OK=0

info() { printf '[+] %s\n' "$*"; }
ok()   { printf '[✓] %s\n' "$*"; }
warn() { printf '[!] %s\n' "$*" >&2; }
die()  { printf '[X] %s\n' "$*" >&2; exit 1; }

cleanup() {
    if [ -n "$BUILD_TMP" ] && [ -d "$BUILD_TMP" ]; then
        rm -rf -- "$BUILD_TMP"
    fi
    if [ -n "$STAGE_TMP" ] && [ -d "$STAGE_TMP" ]; then
        rm -rf -- "$STAGE_TMP"
    fi
}
trap cleanup EXIT

have_systemd() { [ -d /run/systemd/system ]; }

# Family of the running distro: first match over ID, then each ID_LIKE word.
distro_family() {
    local id id_like word
    id="$(sed -n 's/^ID=//p' /etc/os-release /usr/lib/os-release 2>/dev/null | head -n1 | tr -d '"'"'"'' | tr 'A-Z' 'a-z')"
    id_like="$(sed -n 's/^ID_LIKE=//p' /etc/os-release /usr/lib/os-release 2>/dev/null | head -n1 | tr -d '"'"'"'' | tr 'A-Z' 'a-z')"
    for word in $id $id_like; do
        case "$word" in
            arch|manjaro|endeavouros|garuda) echo arch; return ;;
            debian|ubuntu|linuxmint|pop|elementary|kali) echo debian; return ;;
            fedora|rhel|centos|rocky|almalinux|nobara) echo fedora; return ;;
            opensuse*|sles|suse) echo suse; return ;;
            alpine) echo alpine; return ;;
        esac
    done
    echo unknown
}

install_hint() {
    case "$(distro_family)" in
        arch)
            echo "sudo pacman -S --needed iproute2 iptables procps-ng util-linux polkit xdg-utils" ;;
        debian)
            echo "sudo apt install iproute2 iptables procps util-linux policykit-1 xdg-utils" ;;
        fedora)
            echo "sudo dnf install iproute iptables-nft procps-ng util-linux polkit xdg-utils" ;;
        suse)
            echo "sudo zypper install iproute2 iptables procps util-linux polkit xdg-utils" ;;
        alpine)
            echo "sudo apk add iproute2 iptables procps-ng util-linux-login polkit xdg-utils" ;;
        *)
            echo "install iproute2, iptables, procps (sysctl), util-linux (runuser) and polkit (package names vary by distro)" ;;
    esac
}

tkinter_hint() {
    case "$(distro_family)" in
        arch)   echo "sudo pacman -S --needed tk" ;;
        debian) echo "sudo apt install python3-tk python3-venv" ;;
        fedora) echo "sudo dnf install python3-tkinter" ;;
        suse)   echo "sudo zypper install python3-tk" ;;
        alpine) echo "sudo apk add py3-tkinter" ;;
        *)      echo "install the Tk bindings for Python 3 (python3-tk / python3-tkinter / tk)" ;;
    esac
}

# Looks in the usual sbin directories too: sudo on some distros has no sbin in PATH.
have_tool() {
    local tool="$1" dir
    command -v "$tool" >/dev/null 2>&1 && return 0
    for dir in /usr/local/sbin /usr/sbin /sbin; do
        [ -x "$dir/$tool" ] && return 0
    done
    return 1
}

check_tools() {
    local tool
    local missing=()
    for tool in ip iptables sysctl runuser pkexec; do
        have_tool "$tool" || missing+=("$tool")
    done
    if [ "${#missing[@]}" -eq 0 ]; then
        ok "Runtime tools found"
    else
        warn "Missing runtime tools: ${missing[*]}"
        warn "Install hint: $(install_hint)"
    fi
}

require_sources() {
    local f
    for f in \
        packaging/linux/org.intermux.policy \
        packaging/linux/intermux.desktop \
        packaging/linux/intermux-watch.service \
        packaging/linux/intermux.svg \
        packaging/linux/intermux.metainfo.xml; do
        [ -f "$SCRIPT_DIR/$f" ] || die "Missing $f next to install.sh"
    done
    # Only needed when the bundle is built from source (a release tarball has no sources).
    if [ ! -x "$SCRIPT_DIR/dist/intermux/intermux" ]; then
        for f in intermux_linux.spec intermux/__main__.py; do
            [ -f "$SCRIPT_DIR/$f" ] || die "Missing $f next to install.sh (and no prebuilt dist/intermux/)"
        done
    fi
}

# Builds the onedir bundle as $SUDO_USER. Sets BUILD_TMP and BUILD_DIST.
build_onedir() {
    local user="$SUDO_USER"
    local py venv

    py="$(command -v python3 || true)"
    [ -n "$py" ] || die "python3 not found. Install Python 3 and the venv module (python3-venv on Debian/Ubuntu)."

    # The GUI is built from this interpreter's tkinter; PyInstaller only warns when it is missing.
    sudo -u "$user" -- "$py" -c 'import tkinter' >/dev/null 2>&1 \
        || die "python3 cannot import tkinter. Install it first: $(tkinter_hint)"

    info "Building InterMux as user '$user' in a temporary directory"
    BUILD_TMP="$(sudo -u "$user" -- mktemp -d)"
    venv="$BUILD_TMP/venv"

    sudo -u "$user" -- "$py" -m venv "$venv" \
        || die "Could not create a virtualenv (is the venv module installed?)"
    # Supply chain note: the version is pinned (6.20.0 is the version verified to
    # build InterMux) but hashes are not verified. Follow-up: use a hash file with
    # `pip install --require-hashes -r ...`. The result is re-staged under root
    # ownership and printed with its checksum before it is installed.
    sudo -u "$user" -- "$venv/bin/pip" install --quiet --disable-pip-version-check \
        "pyinstaller==$PYINSTALLER_VERSION" \
        || die "Could not install pyinstaller==$PYINSTALLER_VERSION"

    (
        cd -- "$SCRIPT_DIR"
        # Keep PyInstaller's cache inside the build tree so $HOME is not needed.
        sudo -u "$user" -- env PYINSTALLER_CONFIG_DIR="$BUILD_TMP/pyi-config" \
            "$venv/bin/pyinstaller" intermux_linux.spec \
            --noconfirm \
            --distpath "$BUILD_TMP/dist" \
            --workpath "$BUILD_TMP/build"
    ) || die "PyInstaller build failed"

    BUILD_DIST="$BUILD_TMP/dist/intermux"
    [ -x "$BUILD_DIST/intermux" ] || die "PyInstaller did not produce $BUILD_DIST/intermux"
    ok "Built $BUILD_DIST"

    # Freeze a root-owned copy so the user cannot swap files between build and install.
    STAGE_TMP="$(mktemp -d)"
    cp -r --no-preserve=ownership -- "$BUILD_DIST/." "$STAGE_TMP/"
    chown -R root:root -- "$STAGE_TMP"
    chmod -R go-w -- "$STAGE_TMP"
    BUILD_DIST="$STAGE_TMP"
    info "Staged bundle SHA-256 (tree): $(bundle_checksum "$BUILD_DIST")"
}

# Tree checksum: sha256 over the sorted "sha256sum" listing of every regular
# file, paths relative to the bundle root. The same value goes into
# dist/intermux.sha256 (written by the release workflow).
bundle_checksum() {
    ( cd -- "$1" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1 )
}

# True when every entry in the bundle is root-owned and not group/world-writable.
bundle_is_root_owned() {
    [ -d "$1" ] || return 1
    local bad
    bad="$(find "$1" \( ! -uid 0 -o -perm /022 \) -print -quit)" || return 1
    [ -z "$bad" ]
}

# Decide whether the prebuilt dist/ bundle may be installed (it will run as root).
vet_prebuilt_bundle() {
    local dir="$1" sum sumfile expected
    sum="$(bundle_checksum "$dir")"
    sumfile="$SCRIPT_DIR/dist/intermux.sha256"
    expected="$EXPECT_SHA256"
    if [ -z "$expected" ] && [ -f "$sumfile" ]; then
        expected="$(tr -d ' \t\r\n' < "$sumfile" | cut -c1-64)"
    fi
    info "Prebuilt bundle SHA-256 (tree): $sum"
    if [ -n "$expected" ] && [ "$expected" != "$sum" ]; then
        die "Prebuilt bundle checksum mismatch (expected $expected). Refusing to install."
    fi
    if bundle_is_root_owned "$dir"; then
        ok "Prebuilt bundle is root-owned and not writable by others"
        return 0
    fi
    if [ "$ALLOW_LOCAL_BUNDLE" -ne 1 ]; then
        warn "The prebuilt bundle in $dir is not root-owned: anyone who can write to it controls code that runs as root."
        warn "Review the checksum above, then re-run with --allow-local-bundle to install it,"
        warn "or remove dist/ to build from source, or unpack a release tarball with sudo (root-owned)."
        die "Refusing to install an unverified user-writable bundle."
    fi
    warn "Installing a user-writable bundle because --allow-local-bundle was given"
}

# Privilege note: the polkit action org.intermux.run authorises running the
# installed binary ($INSTALL_LIB/intermux) through pkexec, not only its
# `helper` subcommand. InterMux calls it as `intermux helper`. As root the
# binary refuses every command except `helper` and `watch`, and refuses
# `watch` when started through pkexec (intermux/__main__.py).
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

    # A running watcher executes from $INSTALL_LIB; stop it before the bundle is replaced.
    if have_systemd; then
        systemctl stop intermux-watch.service >/dev/null 2>&1 || true
    fi

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

    # Routing is reset automatically when a routed app exits: the watcher runs `intermux watch` as root.
    install -Dm644 "$SCRIPT_DIR/packaging/linux/intermux-watch.service" "$WATCH_UNIT"
    if have_systemd; then
        WATCHER_OK=1
        systemctl daemon-reload || { warn "systemctl daemon-reload failed"; WATCHER_OK=0; }
        systemctl enable intermux-watch.service || { warn "could not enable intermux-watch.service"; WATCHER_OK=0; }
        systemctl restart intermux-watch.service || { warn "could not start intermux-watch.service"; WATCHER_OK=0; }
        if [ "$WATCHER_OK" -eq 1 ]; then
            ok "Installed and started the routing watcher (intermux-watch.service)"
        else
            warn "The routing watcher is NOT running: routing will not reset automatically when an app exits."
            warn "Fix the error above, or run 'systemctl enable --now intermux-watch.service'."
        fi
    else
        warn "systemd is not running (no /run/systemd/system): the routing watcher was NOT started."
        warn "Routing will not reset automatically; run 'intermux watch' as root from your init system instead."
    fi

    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database -q "$APPS_DIR" || warn "update-desktop-database failed"
    fi
    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
        gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || warn "gtk-update-icon-cache failed"
    fi
}

usage() {
    echo "Usage: sudo ./install.sh [--allow-local-bundle] [--bundle-sha256 HEX]"
}

main() {
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --allow-local-bundle) ALLOW_LOCAL_BUNDLE=1 ;;
            --bundle-sha256)
                [ "$#" -ge 2 ] || die "--bundle-sha256 needs a value"
                EXPECT_SHA256="$2"; shift ;;
            -h|--help) usage; exit 0 ;;
            *) usage >&2; die "Unknown option: $1" ;;
        esac
        shift
    done
    [ "$(uname -s)" = "Linux" ] || die "This installer only supports Linux."
    [ "$(id -u)" -eq 0 ] || die "Please run as root (sudo ./install.sh)"

    info "Installing InterMux"
    require_sources
    check_tools

    local dist_dir
    if [ -x "$SCRIPT_DIR/dist/intermux/intermux" ]; then
        dist_dir="$SCRIPT_DIR/dist/intermux"
        info "Found prebuilt bundle in $dist_dir"
        vet_prebuilt_bundle "$dist_dir"
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
