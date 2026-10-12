#!/usr/bin/env bash
# Remove a system-wide InterMux install. Run as root: sudo ./uninstall.sh [--force]
# If 'intermux reset' fails the script stops; --force removes files anyway.
set -euo pipefail

readonly INSTALL_LIB="/usr/lib/intermux"
readonly INSTALL_BIN="/usr/bin/intermux"
readonly LEGACY_BIN="/usr/local/bin/intermux"
readonly POLKIT_POLICY="/usr/share/polkit-1/actions/org.intermux.policy"
readonly DESKTOP_FILE="/usr/share/applications/intermux.desktop"
readonly ICON_FILE="/usr/share/icons/hicolor/scalable/apps/intermux.svg"
readonly METAINFO_FILE="/usr/share/metainfo/org.intermux.InterMux.metainfo.xml"
readonly STATE_DIR="/var/lib/intermux"
readonly RUN_DIR="/run/intermux"

info() { printf '[+] %s\n' "$*"; }
ok()   { printf '[✓] %s\n' "$*"; }
warn() { printf '[!] %s\n' "$*" >&2; }
die()  { printf '[X] %s\n' "$*" >&2; exit 1; }

main() {
    local force=0
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --force) force=1 ;;
            *) die "Unknown option: $1 (usage: sudo ./uninstall.sh [--force])" ;;
        esac
        shift
    done

    [ "$(id -u)" -eq 0 ] || die "Please run as root (sudo ./uninstall.sh)"

    if [ -x "$INSTALL_BIN" ]; then
        info "Removing InterMux bindings, namespaces and running apps"
        if ! "$INSTALL_BIN" reset --kill-apps --yes; then
            if [ "$force" -ne 1 ]; then
                die "'intermux reset' failed; files were not removed. Fix the reported problem and re-run, or re-run with --force to remove files anyway (this can orphan namespaces, NAT rules and ip_forward state)."
            fi
            warn "'intermux reset' reported errors; continuing because --force was given"
        fi
    else
        warn "$INSTALL_BIN not found; skipping 'intermux reset'"
    fi

    info "Removing installed files"
    rm -rf -- "$INSTALL_LIB"
    rm -f -- "$INSTALL_BIN" "$LEGACY_BIN"
    rm -f -- "$POLKIT_POLICY" "$DESKTOP_FILE" "$ICON_FILE" "$METAINFO_FILE"

    info "Removing state and runtime data"
    rm -rf -- "$STATE_DIR" "$RUN_DIR"

    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database -q /usr/share/applications || warn "update-desktop-database failed"
    fi
    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
        gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || warn "gtk-update-icon-cache failed"
    fi

    ok "InterMux has been removed"
}

main "$@"
