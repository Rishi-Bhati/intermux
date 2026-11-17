#!/bin/bash
# polkit-e2e.sh — runs INSIDE a Docker container.
# Usage:
#   polkit-e2e.sh manual
#   polkit-e2e.sh tarball /out/intermux-linux-x86_64.tar.gz
#
# Tests polkit rules for the InterMux passwordless feature.
# Prints STEP n: ... before each step, OK / FAIL: reason after, exits non-zero on first failure.

set -euo pipefail

MODE="${1:-}"
TARBALL="${2:-}"

if [ "$MODE" != "manual" ] && [ "$MODE" != "tarball" ]; then
    echo "Usage: $0 manual | $0 tarball /out/intermux-linux-x86_64.tar.gz" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
step_n=0
step() {
    step_n=$((step_n + 1))
    echo ""
    echo "STEP $step_n: $*"
}

ok() { echo "  OK"; }

fail() {
    echo "  FAIL: $*"
    exit 1
}

# Non-fatal skip: used when a distro genuinely cannot run polkitd in a container.
skip() {
    echo "  SKIP: $*"
    # Marker line for run-all.sh to detect.
    echo "SKIP: $*"
    exit 0
}

assert() {
    # assert <description> <exit-code-expression>
    local desc="$1"; shift
    if eval "$@"; then
        echo "    ASSERT OK: $desc"
    else
        fail "$desc"
    fi
}

# Wait up to N seconds for a command to exit 0.
wait_for() {
    local secs="$1"; shift
    local i=0
    while [ $i -lt "$secs" ]; do
        if eval "$@" >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
        i=$((i + 1))
    done
    return 1
}

# Run a shell command as a specific user. Uses runuser when available (no PAM
# session setup issues in containers), falls back to su.
run_as() {
    local user="$1"; shift
    local cmd="$*"
    if command -v runuser >/dev/null 2>&1; then
        runuser -u "$user" -- sh -c "$cmd"
    else
        su "$user" -s /bin/sh -c "$cmd"
    fi
}

# ---------------------------------------------------------------------------
# Distro detection
# ---------------------------------------------------------------------------
# shellcheck disable=SC1091
. /etc/os-release
DISTRO_ID="${ID:-unknown}"
DISTRO_VERSION_ID="${VERSION_ID:-}"
echo "Detected distro: $DISTRO_ID $DISTRO_VERSION_ID"

# ---------------------------------------------------------------------------
# STEP 1: Install packages
# ---------------------------------------------------------------------------
step "Install packages"

install_quiet() {
    local out rc
    case "$DISTRO_ID" in
        ubuntu|debian)
            if out=$(DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "$@" 2>&1); then
                rc=0
            else
                rc=$?
            fi
            ;;
        fedora)
            # Avoid weak deps for faster, more predictable installs in minimal containers.
            if out=$(dnf install -y -q --setopt=install_weak_deps=False "$@" 2>&1); then
                rc=0
            else
                rc=$?
            fi
            ;;
        arch)
            if out=$(pacman -S --noconfirm --needed "$@" 2>&1); then
                rc=0
            else
                rc=$?
            fi
            ;;
        *)
            fail "Unknown distro: $DISTRO_ID"
            ;;
    esac
    if [ "$rc" -ne 0 ]; then
        echo "$out" >&2
        fail "package install failed ($DISTRO_ID, rc=$rc, pkgs: $*)"
    fi
}

case "$DISTRO_ID" in
    ubuntu)
        # Ubuntu 22.04: polkit 0.105 (pkla backend)
        if ! apt-get -qq update >/dev/null 2>&1; then
            fail "apt-get update failed on ubuntu"
        fi
        install_quiet dbus policykit-1 passwd procps iproute2 iptables
        ;;
    debian)
        # Debian 12: polkit 122+ (JS rules backend)
        if ! apt-get -qq update >/dev/null 2>&1; then
            fail "apt-get update failed on debian"
        fi
        install_quiet dbus polkitd pkexec passwd procps iproute2 iptables
        ;;
    fedora)
        # Fedora 41: polkit 125 (JS rules backend); dbus-daemon/util-linux provide needed tools
        install_quiet polkit dbus-daemon passwd procps shadow-utils util-linux iproute iptables
        ;;
    arch)
        # Arch: polkit 128 (JS rules backend); pacman -Sy to sync db
        if ! pacman -Sy --noconfirm >/dev/null 2>&1; then
            fail "pacman -Sy failed on arch"
        fi
        install_quiet polkit dbus shadow procps-ng util-linux iproute2 iptables
        ;;
    *)
        fail "Unsupported distro: $DISTRO_ID"
        ;;
esac

ok

# ---------------------------------------------------------------------------
# STEP 2: Create users
# ---------------------------------------------------------------------------
step "Create users tester, tester2, tester3"

for u in tester tester2 tester3; do
    if ! id "$u" >/dev/null 2>&1; then
        useradd -m "$u" >/dev/null 2>&1 || useradd "$u" >/dev/null 2>&1
    fi
done

id tester >/dev/null 2>&1 || fail "user tester not created"
id tester2 >/dev/null 2>&1 || fail "user tester2 not created"
id tester3 >/dev/null 2>&1 || fail "user tester3 not created"
ok

# ---------------------------------------------------------------------------
# STEP 3: Install rule / marker / tarball
# ---------------------------------------------------------------------------
step "Install polkit rule and marker ($MODE mode)"

# Detect polkit backend by pkaction version.
detect_backend() {
    local ver
    if ver=$(pkaction --version 2>/dev/null | awk '{print $NF}'); then
        # Strip any non-numeric suffix (e.g. "0.105" -> major.minor)
        local major minor
        major=$(echo "$ver" | cut -d. -f1)
        minor=$(echo "$ver" | cut -d. -f2)
        # polkit >= 0.106 or >= 106 uses JS rules
        if [ "$major" -ge 1 ] 2>/dev/null; then
            echo "rules"
        elif [ "$major" -eq 0 ] && [ "${minor:-0}" -ge 106 ] 2>/dev/null; then
            echo "rules"
        elif [ "$major" -ge 106 ] 2>/dev/null && [ -z "$minor" ]; then
            # New versioning: "122", "125", "128" — all >= 106
            echo "rules"
        else
            echo "pkla"
        fi
    elif [ -d /etc/polkit-1/localauthority ] && \
         [ ! -d /etc/polkit-1/rules.d ] && \
         [ ! -d /usr/share/polkit-1/rules.d ]; then
        echo "pkla"
    else
        echo "rules"
    fi
}

# Re-detect: pkaction major version alone is enough.
# Version "0.105" => pkla; "0.106", "122", "125", "128" => rules.
detect_backend_v2() {
    local verstr
    verstr=$(pkaction --version 2>/dev/null | awk '{print $NF}')
    if [ -z "$verstr" ]; then
        # Fallback by filesystem
        if [ -d /etc/polkit-1/localauthority ] && [ ! -d /etc/polkit-1/rules.d ]; then
            echo "pkla"; return
        fi
        echo "rules"; return
    fi

    # Version is either "0.105", "0.106", or plain int like "122"
    local dot_count
    dot_count=$(echo "$verstr" | tr -cd '.' | wc -c)
    if [ "$dot_count" -gt 0 ]; then
        # Old-style "major.minor"
        local major minor
        major=$(echo "$verstr" | cut -d. -f1)
        minor=$(echo "$verstr" | cut -d. -f2)
        if [ "$major" -gt 0 ] 2>/dev/null || [ "${minor:-0}" -ge 106 ] 2>/dev/null; then
            echo "rules"
        else
            echo "pkla"
        fi
    else
        # New-style plain integer: always >= 106
        echo "rules"
    fi
}

BACKEND=$(detect_backend_v2)
echo "  polkit backend: $BACKEND"

RULE_JS="/etc/polkit-1/rules.d/49-intermux-passwordless.rules"
RULE_PKLA="/etc/polkit-1/localauthority/50-local.d/49-intermux-passwordless.pkla"
MARKER="/var/lib/intermux/passwordless.json"

# Write rule and marker files exactly as the contract specifies.
write_rule_js() {
    mkdir -p "$(dirname "$RULE_JS")"
    cat > "$RULE_JS" << 'JSEOF'
// Installed by "intermux passwordless on". Remove with: sudo intermux passwordless off --all
// Members of the intermux group run the InterMux helper without a password, from a local, active session only.
polkit.addRule(function (action, subject) {
    if (action.id == "org.intermux.run" && subject.isInGroup("intermux") &&
            subject.local && subject.active) {
        return polkit.Result.YES;
    }
});
JSEOF
    chmod 0644 "$RULE_JS"
}

write_rule_pkla() {
    mkdir -p "$(dirname "$RULE_PKLA")"
    cat > "$RULE_PKLA" << 'PKLAEOF'
# Installed by "intermux passwordless on". Remove with: sudo intermux passwordless off --all
[InterMux passwordless helper]
Identity=unix-group:intermux
Action=org.intermux.run
ResultActive=yes
PKLAEOF
    chmod 0644 "$RULE_PKLA"
}

write_marker() {
    local backend="$1"
    local rule_path="$2"
    local group_created="${3:-true}"
    mkdir -p "$(dirname "$MARKER")"
    # Write atomically: temp file + rename
    local tmp
    tmp="$(dirname "$MARKER")/.marker.tmp.$$"
    printf '{"v": 1, "backend": "%s", "rule_path": "%s", "group_created": %s}\n' \
        "$backend" "$rule_path" "$group_created" > "$tmp"
    chmod 0644 "$tmp"
    mv "$tmp" "$MARKER"
}

if [ "$MODE" = "manual" ]; then
    # Create group intermux and add tester
    if ! getent group intermux >/dev/null 2>&1; then
        groupadd --system intermux
    fi
    gpasswd -a tester intermux >/dev/null 2>&1

    # Write rule file for the detected backend
    if [ "$BACKEND" = "rules" ]; then
        write_rule_js
        write_marker "rules" "$RULE_JS" "true"
    else
        write_rule_pkla
        write_marker "pkla" "$RULE_PKLA" "true"
    fi
    ok

else  # tarball mode
    # Unpack tarball and run install.sh --passwordless
    if [ -z "$TARBALL" ] || [ ! -f "$TARBALL" ]; then
        fail "tarball not found: $TARBALL"
    fi
    WORKDIR=$(mktemp -d)
    tar -xzf "$TARBALL" -C "$WORKDIR"

    # Tarball may extract into a top-level directory or directly into WORKDIR.
    INSTALL_SH=$(find "$WORKDIR" -name 'install.sh' -type f 2>/dev/null | head -1)
    if [ -z "$INSTALL_SH" ]; then
        fail "install.sh not found in tarball (extracted to $WORKDIR)"
    fi
    INSTALL_DIR=$(dirname "$INSTALL_SH")
    cd "$INSTALL_DIR"
    echo "  Extracted tarball; install dir: $INSTALL_DIR"
    ls -la

    TESTER_UID=$(id -u tester)
    echo "  Running install.sh --passwordless as SUDO_USER=tester ..."
    if env SUDO_USER=tester SUDO_UID="$TESTER_UID" ./install.sh --passwordless </dev/null; then
        INSTALL_RC=0
    else
        INSTALL_RC=$?
    fi
    cd /
    if [ "$INSTALL_RC" -ne 0 ]; then
        fail "install.sh --passwordless failed (exit $INSTALL_RC)"
    fi

    # Assert rule file installed
    if [ "$BACKEND" = "rules" ]; then
        [ -f "$RULE_JS" ] || fail "JS rule file not found: $RULE_JS"
    else
        [ -f "$RULE_PKLA" ] || fail "pkla rule file not found: $RULE_PKLA"
    fi
    # Assert marker installed
    [ -f "$MARKER" ] || fail "marker not found: $MARKER"
    # Assert group exists
    getent group intermux >/dev/null 2>&1 || fail "group intermux does not exist"
    # Assert tester is in group
    id -nG tester | tr ' ' '\n' | grep -q '^intermux$' || \
        fail "tester not in intermux group"

    # Assert intermux passwordless status --json for tester
    echo "  Checking 'intermux passwordless status --json' for tester ..."
    if STATUS_OUT=$(run_as tester "intermux passwordless status --json" 2>&1); then
        STATUS_RC=0
    else
        STATUS_RC=$?
    fi
    echo "  tester status (exit $STATUS_RC): $STATUS_OUT"
    echo "$STATUS_OUT" | grep -q '"enabled": true' || \
        fail "tester status should show enabled:true, got: $STATUS_OUT"

    # Assert tester2 status shows enabled:false
    if STATUS2_OUT=$(run_as tester2 "intermux passwordless status --json" 2>&1); then
        STATUS2_RC=0
    else
        STATUS2_RC=$?
    fi
    echo "  tester2 status (exit $STATUS2_RC): $STATUS2_OUT"
    echo "$STATUS2_OUT" | grep -q '"enabled": false' || \
        fail "tester2 status should show enabled:false, got: $STATUS2_OUT"

    # Assert pkexec refusal: PKEXEC_UID=0 must be refused (exit non-zero)
    if PKEXEC_RESULT=$(env PKEXEC_UID=0 /usr/lib/intermux/intermux passwordless on --user tester3 2>&1 </dev/null); then
        PKEXEC_RC=0
    else
        PKEXEC_RC=$?
    fi
    echo "  pkexec refusal test output (exit $PKEXEC_RC): $PKEXEC_RESULT"
    if [ "$PKEXEC_RC" -eq 0 ]; then
        fail "pkexec invocation should be refused (exit non-zero)"
    fi

    ok
fi

# ---------------------------------------------------------------------------
# STEP 4: Start D-Bus and polkitd; install org.intermux.policy in manual mode
# ---------------------------------------------------------------------------
step "Start D-Bus and polkitd"

mkdir -p /run/dbus /var/lib/dbus
# Ensure machine-id exists (some minimal container images lack it)
if command -v dbus-uuidgen >/dev/null 2>&1; then
    dbus-uuidgen --ensure=/etc/machine-id >/dev/null 2>&1 || true
    if [ -f /etc/machine-id ] && [ ! -f /var/lib/dbus/machine-id ]; then
        cp /etc/machine-id /var/lib/dbus/machine-id 2>/dev/null || true
    fi
fi

dbus-daemon --system --fork
sleep 1

# In manual mode, install the policy file from the mounted repo
if [ "$MODE" = "manual" ]; then
    if [ ! -d /repo/packaging/linux ]; then
        fail "Repo not mounted at /repo. Run with -v /home/rishi/Documents/Stuff/intermux:/repo:ro"
    fi
    mkdir -p /usr/share/polkit-1/actions
    cp /repo/packaging/linux/org.intermux.policy /usr/share/polkit-1/actions/
fi

# Find polkitd binary
POLKITD=""
for candidate in \
    /usr/lib/polkit-1/polkitd \
    /usr/libexec/polkitd \
    /usr/lib/policykit-1/polkitd; do
    if [ -x "$candidate" ]; then
        POLKITD="$candidate"
        break
    fi
done
[ -n "$POLKITD" ] || fail "polkitd not found (tried /usr/lib/polkit-1/polkitd, /usr/libexec/polkitd, /usr/lib/policykit-1/polkitd)"
echo "  polkitd: $POLKITD"

# Start polkitd in background, log to /tmp/polkitd.log
: >/tmp/polkitd.log
"$POLKITD" --no-debug >>/tmp/polkitd.log 2>&1 &
POLKITD_PID=$!
echo "  polkitd pid: $POLKITD_PID"

# Wait up to 10 s for polkitd to be ready.
# Ubuntu 22.04's pkaction does not list org.freedesktop.policykit.exec, so
# wait until plain `pkaction` (list all actions) produces output, polling up to 10 s.
# (Some builds may exit non-zero while still returning the action list.)
pkaction_ready() {
    if command -v timeout >/dev/null 2>&1; then
        timeout 3 pkaction >/tmp/pkaction.out 2>&1 || true
    else
        pkaction >/tmp/pkaction.out 2>&1 || true
    fi
    [ -s /tmp/pkaction.out ]
}
echo "  Waiting for polkitd to answer pkaction ..."
if ! wait_for 10 pkaction_ready; then
    echo "  polkitd log:" >&2
    cat /tmp/polkitd.log >&2 || true
    skip "polkitd did not become ready within 10 s in this container environment"
fi
echo "  polkitd is ready"
ok

# ---------------------------------------------------------------------------
# STEP 5: Check rule loaded without errors (JS backend)
# ---------------------------------------------------------------------------
step "Check polkitd log for errors (JS backend only)"

if [ "$BACKEND" = "rules" ]; then
    if grep -qi "Error" /tmp/polkitd.log 2>/dev/null; then
        echo "  WARNING: 'Error' found in polkitd log:"
        grep -i "Error" /tmp/polkitd.log || true
        fail "polkitd logged an error while loading rules"
    fi
    echo "  polkitd log (first 30 lines):"
    head -30 /tmp/polkitd.log 2>/dev/null || echo "  (empty log)"
    # Log should mention loading rules when polkitd logs it (varies by version/verbosity).
    # Absence of errors is the key check; treat missing "loading" mention as non-fatal.
    if grep -Eqi 'load.*rule|rule.*load|rules\.d|Registered|Finished loading' /tmp/polkitd.log 2>/dev/null; then
        echo "  No errors detected in polkitd log; rules loading mentioned"
    else
        echo "  No errors detected in polkitd log (no explicit rule-loading message; this can be normal)"
    fi
else
    echo "  pkla backend: nothing to check in polkitd log"
fi
ok

# ---------------------------------------------------------------------------
# STEP 6: Group logic tests
# ---------------------------------------------------------------------------
# Record distro/polkit info for reporting
POLKIT_VERSION=$(pkaction --version 2>/dev/null | awk '{print $NF}' || echo "unknown")
echo "  INFO: distro=$DISTRO_ID $DISTRO_VERSION_ID polkit=$POLKIT_VERSION backend=$BACKEND polkitd=$POLKITD"

step "6a: Real rule only — tester pkcheck must NOT be authorized (no active session)"

# pkcheck exits 0 = authorized, non-zero = not authorized (1/2/3 depending on case)
# In a container there is no logind session, so subject.local/subject.active are false.
# ResultActive=yes (pkla) or the JS local&&active guard means: not authorized here.
if REAL_CHECK=$(run_as tester 'pkcheck --action-id org.intermux.run --process $$' 2>&1); then
    REAL_RC=0
else
    REAL_RC=$?
fi
echo "  pkcheck exit: $REAL_RC  output: $REAL_CHECK"
if [ "$REAL_RC" -eq 0 ]; then
    fail "Real rule should NOT grant in container (no active session), but pkcheck exited 0"
fi
echo "  Correctly NOT authorized with real rule (exit $REAL_RC)"
ok

# ---------------------------------------------------------------------------
step "6b: Test-only rule — tester authorized, tester2 not"

restart_polkitd() {
    # pkla backend may not pick up localauthority changes without a restart.
    if [ -n "${POLKITD_PID:-}" ]; then
        kill "$POLKITD_PID" >/dev/null 2>&1 || true
        # wait for it to exit
        for _ in 1 2 3 4 5 6 7 8 9 10; do
            if kill -0 "$POLKITD_PID" 2>/dev/null; then
                sleep 0.2 2>/dev/null || sleep 1
            else
                break
            fi
        done
        kill -9 "$POLKITD_PID" >/dev/null 2>&1 || true
    fi
    "$POLKITD" --no-debug >>/tmp/polkitd.log 2>&1 &
    POLKITD_PID=$!
    echo "  restarted polkitd pid: $POLKITD_PID"
    if ! wait_for 10 pkaction_ready; then
        echo "  polkitd log:" >&2
        cat /tmp/polkitd.log >&2 || true
        fail "polkitd did not become ready after restart"
    fi
}

# Install a test-only rule without the local/active conditions.
TEST_RULE=""
TEST_PKLA=""
REAL_PKLA_BAK=""
if [ "$BACKEND" = "rules" ]; then
    TEST_RULE="/etc/polkit-1/rules.d/48-e2e-test.rules"
    mkdir -p "$(dirname "$TEST_RULE")"
    cat > "$TEST_RULE" << 'TESTJS'
// E2E test-only rule: grants org.intermux.run to intermux group without session check.
// This file is removed at the end of the test.
polkit.addRule(function (action, subject) {
    if (action.id == "org.intermux.run" && subject.isInGroup("intermux")) {
        return polkit.Result.YES;
    }
});
TESTJS
    chmod 0644 "$TEST_RULE"
else
    # pkla backend: move real pkla aside to avoid precedence/merge ambiguity and
    # install a test-only pkla granting ResultAny=yes for the group.
    TEST_PKLA="/etc/polkit-1/localauthority/50-local.d/48-e2e-test.pkla"
    mkdir -p "$(dirname "$TEST_PKLA")"
    if [ -f "$RULE_PKLA" ]; then
        REAL_PKLA_BAK="${RULE_PKLA}.bak-e2e"
        mv "$RULE_PKLA" "$REAL_PKLA_BAK"
    fi
    cat > "$TEST_PKLA" << 'TESTPKLA'
# E2E test-only pkla: grants org.intermux.run to intermux group regardless of session.
# This file is removed at the end of the test.
[E2E test]
Identity=unix-group:intermux
Action=org.intermux.run
ResultAny=yes
TESTPKLA
    chmod 0644 "$TEST_PKLA"
    # pkla changes are picked up reliably by restarting polkitd.
    restart_polkitd
fi

# Give polkitd time to reload (JS backend watches inotify) / measure pickup time.
RULE_RELOAD_START=$(date +%s%N 2>/dev/null || echo 0)
RULE_RELOAD_SECS="unknown"
RULE_RELOAD_DEADLINE=$(( $(date +%s) + 10 ))
while [ "$(date +%s)" -lt "$RULE_RELOAD_DEADLINE" ]; do
    if run_as tester 'pkcheck --action-id org.intermux.run --process $$' >/dev/null 2>&1; then
        RULE_RELOAD_END=$(date +%s%N 2>/dev/null || echo 0)
        if [ "$RULE_RELOAD_START" != "0" ] && [ "$RULE_RELOAD_END" != "0" ]; then
            RULE_RELOAD_SECS=$(awk "BEGIN {printf \"%.3f\", ($RULE_RELOAD_END - $RULE_RELOAD_START)/1000000000}")
        else
            RULE_RELOAD_SECS="<=poll interval"
        fi
        break
    fi
    sleep 0.2 2>/dev/null || sleep 1
done
echo "  INFO: rule change pickup time (test-only rule): ${RULE_RELOAD_SECS}s"

# tester (in intermux group) should now be authorized
if TESTER_CHECK=$(run_as tester 'pkcheck --action-id org.intermux.run --process $$' 2>&1); then
    TESTER_RC=0
else
    TESTER_RC=$?
fi
echo "  tester pkcheck exit: $TESTER_RC  output: $TESTER_CHECK"
if [ "$TESTER_RC" -ne 0 ]; then
    fail "tester should be authorized with test-only rule (exit should be 0, got $TESTER_RC)"
fi

# tester2 (NOT in intermux group) should NOT be authorized
if TESTER2_CHECK=$(run_as tester2 'pkcheck --action-id org.intermux.run --process $$' 2>&1); then
    TESTER2_RC=0
else
    TESTER2_RC=$?
fi
echo "  tester2 pkcheck exit: $TESTER2_RC  output: $TESTER2_CHECK"
if [ "$TESTER2_RC" -eq 0 ]; then
    fail "tester2 should NOT be authorized (not in intermux group), but pkcheck exited 0"
fi
echo "  tester: authorized (exit 0), tester2: not authorized (exit $TESTER2_RC)"
ok

# ---------------------------------------------------------------------------
step "6c: Membership takes effect without re-login — add tester3 now"

# tester3 is not yet in the group
if BEFORE_CHECK=$(run_as tester3 'pkcheck --action-id org.intermux.run --process $$' 2>&1); then
    BEFORE_RC=0
else
    BEFORE_RC=$?
fi
if [ "$BEFORE_RC" -eq 0 ]; then
    fail "tester3 should NOT be authorized before joining the group"
fi
echo "  tester3 before join: not authorized (exit $BEFORE_RC)  OK"

# Add tester3 to the group NOW (without re-login)
gpasswd -a tester3 intermux >/dev/null 2>&1

# pkcheck consults group membership; verify it takes effect within ~2s.
MEMBER_OK=0
MEMBER_DEADLINE=$(( $(date +%s) + 3 ))
while [ "$(date +%s)" -lt "$MEMBER_DEADLINE" ]; do
    if AFTER_CHECK=$(run_as tester3 'pkcheck --action-id org.intermux.run --process $$' 2>&1); then
        AFTER_RC=0
    else
        AFTER_RC=$?
    fi
    echo "  tester3 after join: pkcheck exit $AFTER_RC  output: $AFTER_CHECK"
    if [ "$AFTER_RC" -eq 0 ]; then
        MEMBER_OK=1
        break
    fi
    sleep 1
done
if [ "$MEMBER_OK" -ne 1 ]; then
    fail "tester3 should be authorized after being added to intermux (last exit $AFTER_RC)"
fi
echo "  Membership took effect without re-login"
ok

# Clean up test-only rule and restore real rule state
if [ "$BACKEND" = "rules" ]; then
    rm -f "$TEST_RULE"
    sleep 1  # allow polkitd to notice the removal
else
    rm -f "$TEST_PKLA"
    if [ -n "$REAL_PKLA_BAK" ] && [ -f "$REAL_PKLA_BAK" ]; then
        mv "$REAL_PKLA_BAK" "$RULE_PKLA"
    fi
    # Restart to ensure pkla state is back to the real rule.
    restart_polkitd
fi

# ---------------------------------------------------------------------------
# STEP 7: tarball-mode off/uninstall tests
# ---------------------------------------------------------------------------
if [ "$MODE" = "tarball" ]; then
    step "7: Run 'intermux passwordless off --all', assert cleanup; then reinstall and uninstall"

    echo "  Running 'intermux passwordless off --all' ..."
    if env -u PKEXEC_UID /usr/lib/intermux/intermux passwordless off --all </dev/null; then
        OFF_RC=0
    else
        OFF_RC=$?
    fi
    if [ "$OFF_RC" -ne 0 ]; then
        fail "'intermux passwordless off --all' exited $OFF_RC"
    fi

    # Assert rule files are gone
    if [ -f "$RULE_JS" ]; then
        fail "JS rule file still exists after 'off --all': $RULE_JS"
    fi
    if [ -f "$RULE_PKLA" ]; then
        fail "pkla rule file still exists after 'off --all': $RULE_PKLA"
    fi
    # Assert marker is gone
    if [ -f "$MARKER" ]; then
        fail "marker still exists after 'off --all': $MARKER"
    fi
    # Assert group is gone (install.sh created it, so group_created=true)
    if getent group intermux >/dev/null 2>&1; then
        fail "intermux group still exists after 'off --all' (expected removal)"
    fi
    echo "  'off --all' cleaned up correctly"

    # Reinstall with --passwordless
    echo "  Reinstalling ..."
    TESTER_UID=$(id -u tester)
    if env SUDO_USER=tester SUDO_UID="$TESTER_UID" "$INSTALL_DIR/install.sh" --passwordless </dev/null; then
        REINSTALL_RC=0
    else
        REINSTALL_RC=$?
    fi
    if [ "$REINSTALL_RC" -ne 0 ]; then
        fail "reinstall with --passwordless failed (exit $REINSTALL_RC)"
    fi
    [ -f "$MARKER" ] || fail "marker not re-created by reinstall"

    # Uninstall with uninstall.sh --force
    echo "  Running uninstall.sh --force ..."
    if "$INSTALL_DIR/uninstall.sh" --force </dev/null; then
        UNINST_RC=0
    else
        UNINST_RC=$?
    fi
    if [ "$UNINST_RC" -ne 0 ]; then
        fail "uninstall.sh --force exited $UNINST_RC"
    fi

    # Assert everything is gone
    if [ -f "$RULE_JS" ];   then fail "JS rule still exists after uninstall"; fi
    if [ -f "$RULE_PKLA" ]; then fail "pkla rule still exists after uninstall"; fi
    if [ -f "$MARKER" ];    then fail "marker still exists after uninstall"; fi
    if getent group intermux >/dev/null 2>&1; then
        fail "intermux group still exists after uninstall"
    fi
    if [ -d /usr/lib/intermux ]; then
        fail "/usr/lib/intermux still exists after uninstall"
    fi
    echo "  uninstall.sh --force cleaned up correctly"
    ok
fi

# ---------------------------------------------------------------------------
echo ""
echo "ALL OK"
