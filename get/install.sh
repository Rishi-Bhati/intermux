#!/bin/sh
# InterMux installer for Linux.
#
#   curl -fsSL https://raw.githubusercontent.com/Rishi-Bhati/intermux/main/get/install.sh | sh
#   wget -qO- https://raw.githubusercontent.com/Rishi-Bhati/intermux/main/get/install.sh | sh
#
# Pass options after "sh -s --", for example:
#
#   curl -fsSL .../install.sh | sh -s -- --version 2.0.1
#   curl -fsSL .../install.sh | sh -s -- --uninstall
#
# What it does:
#   1. Finds the newest Linux release (tags named linux-vX.Y.Z) or the one you ask for.
#   2. Installs the runtime tools InterMux needs (iproute2, iptables, polkit, ...)
#      with your package manager, unless you pass --no-deps.
#   3. On x86_64 with glibc 2.35 or newer, downloads the prebuilt release tarball,
#      checks it against the published SHA-256 and runs its install.sh as root.
#      Anywhere else (aarch64, older glibc, musl) or with --from-source, it downloads
#      the tagged source and lets install.sh build the bundle as your own user.
#
# Everything is wrapped in functions and main runs on the last line, so a
# download cut off halfway does nothing.

set -eu

REPO="Rishi-Bhati/intermux"
GH_URL="https://github.com/$REPO"
API_URL="https://api.github.com/repos/$REPO"
RAW_URL="https://raw.githubusercontent.com/$REPO/main/get/install.sh"
ASSET="intermux-linux-x86_64.tar.gz"
MIN_GLIBC="2.35"

VERSION=""
FROM_SOURCE=0
UNINSTALL=0
INSTALL_DEPS=1
SUDO=""
WORK_DIR=""
ROOT_DIR=""
UNPACKED=""
ARCHIVE=""
TAG=""
APT_UPDATED=0

# ---------------------------------------------------------------- output

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    C_BOLD="$(printf '\033[1m')"; C_DIM="$(printf '\033[2m')"
    C_RED="$(printf '\033[31m')"; C_GREEN="$(printf '\033[32m')"
    C_YELLOW="$(printf '\033[33m')"; C_BLUE="$(printf '\033[34m')"
    C_RESET="$(printf '\033[0m')"
else
    C_BOLD=""; C_DIM=""; C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_RESET=""
fi

say()  { printf '%s==>%s %s\n' "$C_BLUE$C_BOLD" "$C_RESET" "$*"; }
ok()   { printf '%s ✓ %s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
note() { printf '%s   %s%s\n' "$C_DIM" "$*" "$C_RESET"; }
warn() { printf '%s ! %s %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2; }
die()  { printf '%s ✗ %s %s\n' "$C_RED" "$C_RESET" "$*" >&2; exit 1; }

usage() {
    cat <<EOF
InterMux installer for Linux

Usage:
  curl -fsSL $RAW_URL | sh
  curl -fsSL $RAW_URL | sh -s -- [options]

Options:
  --version X.Y.Z   Install this release instead of the newest one
  --from-source     Build from the tagged source instead of using the prebuilt bundle
  --no-deps         Do not install missing system packages
  --uninstall       Remove InterMux (resets bindings and stops routed apps first)
  -h, --help        Show this help
EOF
}

# ---------------------------------------------------------------- helpers

have() { command -v "$1" >/dev/null 2>&1; }

# Also looks in the sbin directories, which are not on PATH for normal users on some distros.
have_tool() {
    have "$1" && return 0
    for _d in /usr/local/sbin /usr/sbin /sbin; do
        [ -x "$_d/$1" ] && return 0
    done
    return 1
}

as_root() {
    if [ -z "$SUDO" ]; then
        "$@"
    else
        $SUDO "$@"
    fi
}

cleanup() {
    if [ -n "$WORK_DIR" ] && [ -d "$WORK_DIR" ]; then
        rm -rf -- "$WORK_DIR"
    fi
    if [ -n "$ROOT_DIR" ]; then
        as_root rm -rf -- "$ROOT_DIR" 2>/dev/null || true
    fi
}

download() { # URL DEST
    if have curl; then
        curl -fsSL --proto '=https' --tlsv1.2 --retry 3 -o "$2" "$1"
    elif have wget; then
        wget -q --https-only -O "$2" "$1"
    else
        die "Neither curl nor wget is installed."
    fi
}

fetch() { # URL -> stdout
    if have curl; then
        curl -fsSL --proto '=https' --tlsv1.2 --retry 3 "$1"
    else
        wget -q --https-only -O - "$1"
    fi
}

sha256_of() {
    if have sha256sum; then
        sha256sum "$1" | cut -d' ' -f1
    elif have shasum; then
        shasum -a 256 "$1" | cut -d' ' -f1
    elif have openssl; then
        openssl dgst -sha256 "$1" | sed 's/.*= *//'
    else
        die "No SHA-256 tool found (sha256sum, shasum or openssl)."
    fi
}

# True when version $1 is lower than $2 (numeric, dot separated).
version_lt() {
    awk -v a="$1" -v b="$2" 'BEGIN {
        na = split(a, x, "."); nb = split(b, y, ".")
        n = (na > nb) ? na : nb
        for (i = 1; i <= n; i++) {
            if ((x[i] + 0) < (y[i] + 0)) exit 0
            if ((x[i] + 0) > (y[i] + 0)) exit 1
        }
        exit 1
    }'
}

# ---------------------------------------------------------------- checks

setup_root() {
    if [ "$(id -u)" -eq 0 ]; then
        SUDO=""
    elif have sudo; then
        SUDO="sudo"
    elif have doas; then
        SUDO="doas"
    else
        die "InterMux installs system-wide and needs root. Install sudo, or run this as root."
    fi
    if [ -n "$SUDO" ]; then
        note "InterMux installs system-wide, so $SUDO may ask for your password."
        # Ask once up front so the prompt does not land in the middle of the output.
        # sudo reads the password from the terminal, so this works under "curl | sh".
        if [ "$SUDO" = "sudo" ]; then
            sudo -v || die "Could not get root with sudo."
        fi
    fi
}

check_prereqs() {
    [ "$(uname -s)" = "Linux" ] || die "This installer is for Linux. On Windows use install.ps1 (see $GH_URL)."
    have curl || have wget || die "Install curl or wget first."
    for _t in tar gzip mktemp awk sed; do
        have "$_t" || die "'$_t' is required but not installed."
    done
}

# Refuse to overwrite an install that a distro package owns.
check_package_owned() {
    [ -e /usr/lib/intermux ] || return 0
    _owner=""
    if have pacman && pacman -Qqo /usr/lib/intermux >/dev/null 2>&1; then
        _owner="pacman"
    elif have dpkg && dpkg -S /usr/lib/intermux >/dev/null 2>&1; then
        _owner="apt/dpkg"
    elif have rpm && rpm -qf /usr/lib/intermux >/dev/null 2>&1; then
        _owner="rpm"
    fi
    if [ -n "$_owner" ]; then
        die "InterMux is installed by your package manager ($_owner). Update or remove it with $_owner instead."
    fi
}

installed_version() {
    [ -x /usr/lib/intermux/intermux ] || return 0
    # The CLI refuses to run as root, so this only answers for a normal user.
    [ "$(id -u)" -eq 0 ] && return 0
    /usr/lib/intermux/intermux --version 2>/dev/null | awk '{print $NF}' || true
}

glibc_version() {
    getconf GNU_LIBC_VERSION 2>/dev/null | awk '{print $2}' || true
}

choose_mode() {
    [ "$FROM_SOURCE" -eq 1 ] && return 0
    _arch="$(uname -m)"
    _glibc="$(glibc_version)"
    if [ "$_arch" != "x86_64" ]; then
        note "No prebuilt bundle for $_arch; building from source instead."
        FROM_SOURCE=1
    elif [ -z "$_glibc" ]; then
        note "This system does not use glibc (musl?); building from source instead."
        FROM_SOURCE=1
    elif version_lt "$_glibc" "$MIN_GLIBC"; then
        note "glibc $_glibc is older than $MIN_GLIBC, which the prebuilt bundle needs; building from source instead."
        FROM_SOURCE=1
    fi
}

# ---------------------------------------------------------------- release lookup

normalize_version() {
    _v="$1"
    _v="${_v#linux-}"
    _v="${_v#v}"
    printf '%s' "$_v"
}

resolve_tag() {
    if [ -n "$VERSION" ]; then
        TAG="linux-v$(normalize_version "$VERSION")"
        return 0
    fi
    TAG=""
    # releases/latest redirects to the newest full release; no API rate limit.
    if have curl; then
        TAG="$(curl -fsSLI --proto '=https' -o /dev/null -w '%{url_effective}' "$GH_URL/releases/latest" 2>/dev/null \
            | sed -n 's#.*/releases/tag/##p')" || TAG=""
    fi
    case "$TAG" in
        linux-v*) return 0 ;;
    esac
    # Fallback (wget, or a non-Linux release marked latest): newest linux-v* tag from the API.
    TAG="$(fetch "$API_URL/releases?per_page=30" 2>/dev/null \
        | tr ',' '\n' \
        | sed -n 's/.*"tag_name": *"\(linux-v[^"]*\)".*/\1/p' \
        | head -n1)" || TAG=""
    [ -n "$TAG" ] || die "Could not find the newest InterMux release. Try again, or pass --version X.Y.Z."
}

# ---------------------------------------------------------------- dependencies

pkg_manager() {
    for _pm in pacman apt-get dnf yum zypper apk; do
        if have "$_pm"; then echo "$_pm"; return 0; fi
    done
    echo none
}

# Package that provides tool $1 for package manager $2.
pkg_for() {
    case "$2:$1" in
        pacman:ip|apt-get:ip|zypper:ip|apk:ip) echo iproute2 ;;
        dnf:ip|yum:ip) echo iproute ;;
        dnf:iptables|yum:iptables) echo iptables-nft ;;
        *:iptables) echo iptables ;;
        apt-get:sysctl|zypper:sysctl) echo procps ;;
        *:sysctl) echo procps-ng ;;
        apk:runuser) echo util-linux-login ;;
        *:runuser) echo util-linux ;;
        apt-get:pkexec)
            # Debian 12+/Ubuntu 23.04+ ship pkexec on its own; older releases in policykit-1.
            if apt-cache show pkexec >/dev/null 2>&1; then echo pkexec; else echo policykit-1; fi ;;
        *:pkexec) echo polkit ;;
        *:xdg-open) echo xdg-utils ;;
        *:bash) echo bash ;;
        *:objdump) echo binutils ;;
        pacman:python3) echo python ;;
        *:python3) echo python3 ;;
        pacman:tkinter) echo tk ;;
        apt-get:tkinter|zypper:tkinter) echo python3-tk ;;
        dnf:tkinter|yum:tkinter) echo python3-tkinter ;;
        apk:tkinter) echo py3-tkinter ;;
        apt-get:venv) echo python3-venv ;;
    esac
}

pkg_install() { # PM PACKAGES...
    _pm="$1"; shift
    case "$_pm" in
        pacman)
            # No -y: syncing without upgrading is a partial upgrade on Arch.
            as_root pacman -S --needed --noconfirm "$@" \
                || die "pacman could not install:$* (if packages were not found, run 'sudo pacman -Syu' first)" ;;
        apt-get)
            if [ "$APT_UPDATED" -eq 0 ]; then
                as_root env DEBIAN_FRONTEND=noninteractive apt-get update -qq
                APT_UPDATED=1
            fi
            # dpkg's progress goes to stdout; errors still reach the terminal on stderr.
            as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "$@" >/dev/null ;;
        dnf)     as_root dnf install -y -q "$@" ;;
        yum)     as_root yum install -y -q "$@" ;;
        zypper)  as_root zypper --non-interactive --quiet install "$@" ;;
        apk)     as_root apk add --quiet "$@" ;;
    esac
}

missing_tools() {
    _missing=""
    for _t in bash ip iptables sysctl runuser pkexec xdg-open; do
        have_tool "$_t" || _missing="$_missing $_t"
    done
    if [ "$FROM_SOURCE" -eq 1 ]; then
        # PyInstaller needs objdump on Linux.
        have objdump || _missing="$_missing objdump"
        if ! have python3; then
            _missing="$_missing python3 tkinter venv"
        else
            python3 -c 'import tkinter' >/dev/null 2>&1 || _missing="$_missing tkinter"
            python3 -c 'import ensurepip, venv' >/dev/null 2>&1 || _missing="$_missing venv"
        fi
    fi
    echo "$_missing"
}

install_deps() {
    _missing="$(missing_tools)"
    if [ -z "$_missing" ]; then
        ok "Required system tools are already installed"
        return 0
    fi
    _pm="$(pkg_manager)"
    _pkgs=""
    for _t in $_missing; do
        _p="$(pkg_for "$_t" "$_pm")"
        [ -n "$_p" ] || continue
        case " $_pkgs " in *" $_p "*) ;; *) _pkgs="$_pkgs $_p" ;; esac
    done
    if [ "$INSTALL_DEPS" -eq 0 ] || [ "$_pm" = "none" ] || [ -z "$_pkgs" ]; then
        warn "Missing tools:$_missing"
        [ "$_pm" = "none" ] && warn "No supported package manager found; install them yourself."
        [ "$INSTALL_DEPS" -eq 0 ] && [ -n "$_pkgs" ] && warn "Install them with $_pm:$_pkgs"
        # bash is needed to run install.sh at all; python is needed for a source build.
        case " $_missing " in *" bash "*) die "bash is required." ;; esac
        if [ "$FROM_SOURCE" -eq 1 ]; then
            case " $_missing " in *" python3 "*|*" tkinter "*|*" venv "*|*" objdump "*) die "A source build needs python3 with tkinter and venv, and binutils." ;; esac
        fi
        return 0
    fi
    say "Installing system packages:$_pkgs"
    # shellcheck disable=SC2086 # word splitting is intended
    pkg_install "$_pm" $_pkgs || die "Package install failed. Install these yourself and re-run:$_pkgs"
    ok "System packages installed"
}

# PyInstaller needs Python's shared library. Debian and Ubuntu ship it in a
# separate libpython3.X package that python3 does not pull in.
ensure_libpython() {
    if [ "$INSTALL_DEPS" -ne 1 ] || [ "$(pkg_manager)" != "apt-get" ] || ! have python3; then
        return 0
    fi
    python3 - <<'PY' && return 0
import os, sys, sysconfig as s
name = s.get_config_var("INSTSONAME") or ""
libdir = s.get_config_var("LIBDIR") or ""
multi = s.get_config_var("MULTIARCH") or ""
sys.exit(0 if name and any(os.path.exists(os.path.join(d, name)) for d in (libdir, os.path.join(libdir, multi))) else 1)
PY
    _pyver="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    say "Installing system packages: libpython$_pyver"
    pkg_install apt-get "libpython$_pyver" || die "Could not install libpython$_pyver, which PyInstaller needs."
}

# ---------------------------------------------------------------- install / uninstall

# Unpack tarball $1 as root into a fresh root-owned directory; sets UNPACKED to its top-level path.
# The bundle runs as root through polkit, so nothing in it may stay writable by a normal user.
unpack_as_root() {
    _top="$(tar -tzf "$1" | grep -v '^pax_global_header' | head -n1 | cut -d/ -f1)"
    [ -n "$_top" ] || die "The downloaded archive is empty or damaged."
    ROOT_DIR="$(as_root mktemp -d)"
    # Readable (not writable) by others: a source build runs PyInstaller as the invoking user here.
    as_root chmod 755 "$ROOT_DIR"
    as_root tar -xzf "$1" -C "$ROOT_DIR" || die "Could not unpack the downloaded archive."
    as_root chown -R 0:0 "$ROOT_DIR"
    as_root chmod -R go-w "$ROOT_DIR"
    UNPACKED="$ROOT_DIR/$_top"
}

fetch_prebuilt() {
    _url="$GH_URL/releases/download/$TAG/$ASSET"
    say "Downloading $ASSET ($TAG)"
    download "$_url" "$WORK_DIR/$ASSET" || die "Download failed: $_url"
    download "$_url.sha256" "$WORK_DIR/$ASSET.sha256" || die "Download failed: $_url.sha256"
    _want="$(cut -d' ' -f1 < "$WORK_DIR/$ASSET.sha256" | tr -d '\r\n')"
    _got="$(sha256_of "$WORK_DIR/$ASSET")"
    if [ -z "$_want" ] || [ "$_want" != "$_got" ]; then
        die "Checksum mismatch for $ASSET (expected $_want, got $_got). Not installing."
    fi
    ok "Checksum verified ($_got)"
    ARCHIVE="$WORK_DIR/$ASSET"
}

fetch_source() {
    _url="$GH_URL/archive/refs/tags/$TAG.tar.gz"
    say "Downloading InterMux source ($TAG)"
    download "$_url" "$WORK_DIR/source.tar.gz" || die "Download failed: $_url (does release $TAG exist?)"
    ARCHIVE="$WORK_DIR/source.tar.gz"
}

do_install() {
    check_package_owned
    choose_mode
    if [ "$FROM_SOURCE" -eq 1 ] && [ "$(id -u)" -eq 0 ] && { [ -z "${SUDO_USER:-}" ] || [ "${SUDO_USER:-}" = "root" ]; }; then
        die "A source build runs as a normal user. Re-run this from your own account (it will use sudo)."
    fi
    if [ "$FROM_SOURCE" -eq 1 ] && ! have sudo; then
        die "A source build needs sudo (install.sh builds as your user with 'sudo -u')."
    fi

    resolve_tag
    _new="${TAG#linux-v}"
    _old="$(installed_version)"
    if [ "$_old" = "$_new" ]; then
        say "Reinstalling InterMux $_new (already the installed version)"
    elif [ -n "$_old" ]; then
        say "Updating InterMux $_old → $_new"
    elif [ -e /usr/lib/intermux ]; then
        say "Updating InterMux to $_new"
    else
        say "Installing InterMux $_new"
    fi

    # Download first so a wrong --version fails before anything is installed.
    if [ "$FROM_SOURCE" -eq 1 ]; then
        fetch_source
    else
        fetch_prebuilt
    fi

    install_deps
    [ "$FROM_SOURCE" -eq 1 ] && ensure_libpython

    unpack_as_root "$ARCHIVE"
    _dir="$UNPACKED"
    [ -f "$_dir/install.sh" ] || die "install.sh not found in the downloaded archive."

    if [ "$FROM_SOURCE" -eq 1 ]; then
        say "Building and installing (this takes a minute or two)"
    else
        say "Installing"
    fi
    as_root bash "$_dir/install.sh" || die "install.sh failed (see the messages above)."

    printf '\n'
    ok "${C_BOLD}InterMux $_new is installed.${C_RESET}"
    note "Open InterMux from your application menu, or run: intermux"
    note "Update later by running this installer again."
    note "Uninstall: curl -fsSL $RAW_URL | sh -s -- --uninstall"
}

do_uninstall() {
    if [ ! -e /usr/lib/intermux ] && [ ! -e /usr/bin/intermux ]; then
        ok "InterMux is not installed."
        return 0
    fi
    check_package_owned
    resolve_tag
    fetch_source
    unpack_as_root "$ARCHIVE"
    _dir="$UNPACKED"
    [ -f "$_dir/uninstall.sh" ] || die "uninstall.sh not found in the downloaded source."
    say "Removing InterMux"
    as_root bash "$_dir/uninstall.sh" || die "uninstall.sh failed (see above). Re-run it with --force to remove the files anyway: sudo bash $_dir/uninstall.sh --force"
    ok "InterMux has been removed."
}

main() {
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --version)
                [ "$#" -ge 2 ] || die "--version needs a value, e.g. --version 2.0.1"
                VERSION="$2"; shift ;;
            --version=*) VERSION="${1#*=}" ;;
            --from-source) FROM_SOURCE=1 ;;
            --no-deps) INSTALL_DEPS=0 ;;
            --uninstall) UNINSTALL=1 ;;
            -h|--help) usage; exit 0 ;;
            *) usage >&2; die "Unknown option: $1" ;;
        esac
        shift
    done

    check_prereqs
    setup_root
    WORK_DIR="$(mktemp -d)"
    trap cleanup EXIT
    trap 'exit 130' INT TERM
    trap 'exit 141' HUP PIPE

    if [ "$UNINSTALL" -eq 1 ]; then
        do_uninstall
    else
        do_install
    fi
}

main "$@"
