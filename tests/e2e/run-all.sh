#!/bin/bash
# run-all.sh — host-side runner; executes polkit-e2e.sh in each Docker image.
# Usage:
#   run-all.sh manual
#   run-all.sh tarball <dir containing intermux-linux-x86_64.tar.gz>
#
# Prints one PASS / FAIL / SKIP line per image.

set -euo pipefail

MODE="${1:-}"
TARBALL_DIR="${2:-}"

if [ "$MODE" != "manual" ] && [ "$MODE" != "tarball" ]; then
    echo "Usage: $0 manual | $0 tarball <dir>" >&2
    exit 1
fi

if [ "$MODE" = "tarball" ]; then
    if [ -z "$TARBALL_DIR" ] || [ ! -d "$TARBALL_DIR" ]; then
        echo "ERROR: tarball directory not found: $TARBALL_DIR" >&2
        exit 1
    fi
    TARBALL="$TARBALL_DIR/intermux-linux-x86_64.tar.gz"
    if [ ! -f "$TARBALL" ]; then
        echo "ERROR: tarball not found: $TARBALL" >&2
        exit 1
    fi
fi

# Directory that contains this script (the e2e dir)
E2E_DIR="$(cd "$(dirname "$0")" && pwd)"

# Intermux repository path (mounted read-only for the policy file and manual mode)
REPO_DIR="$(cd "$E2E_DIR/../.." && pwd)"

IMAGES=(
    "ubuntu:22.04"
    "debian:12"
    "fedora:41"
    "archlinux:latest"
)

RESULTS=()
OVERALL_RC=0

echo "============================================================"
echo "InterMux polkit E2E — mode: $MODE"
echo "E2E dir: $E2E_DIR"
echo "============================================================"
echo ""

for IMAGE in "${IMAGES[@]}"; do
    echo "------------------------------------------------------------"
    echo "IMAGE: $IMAGE"
    echo "------------------------------------------------------------"

    # Build the docker run command.
    # Always mount the e2e dir (read-only) and the repo (read-only for policy file).
    DOCKER_ARGS=(
        "docker" "run" "--rm"
        "--privileged"            # polkitd needs to open /dev/null etc. in some distros
        "-v" "${E2E_DIR}:/e2e:ro"
        "-v" "${REPO_DIR}:/repo:ro"
    )

    if [ "$MODE" = "tarball" ]; then
        TARBALL_ABS="$(cd "$(dirname "$TARBALL")" && pwd)/$(basename "$TARBALL")"
        TARBALL_CONTAINER_DIR="/out"
        DOCKER_ARGS+=("-v" "$(dirname "$TARBALL_ABS"):${TARBALL_CONTAINER_DIR}:ro")
        CONTAINER_CMD=("bash" "/e2e/polkit-e2e.sh" "tarball" "${TARBALL_CONTAINER_DIR}/intermux-linux-x86_64.tar.gz")
    else
        CONTAINER_CMD=("bash" "/e2e/polkit-e2e.sh" "manual")
    fi

    DOCKER_ARGS+=("$IMAGE")
    DOCKER_ARGS+=("${CONTAINER_CMD[@]}")

    # Run and capture output + exit code
    LOG_FILE="$(mktemp /tmp/polkit-e2e-${IMAGE//[:\/ ]/_}-XXXXXX.log)"
    RC=0
    "${DOCKER_ARGS[@]}" 2>&1 | tee "$LOG_FILE" || RC=$?

    # Determine result
    if grep -q "^SKIP:" "$LOG_FILE"; then
        SKIP_REASON=$(grep "^SKIP:" "$LOG_FILE" | head -1 | sed 's/^SKIP: //')
        RESULT="SKIP: $SKIP_REASON"
    elif [ $RC -eq 0 ] && grep -q "^ALL OK" "$LOG_FILE"; then
        RESULT="PASS"
    else
        # Extract the first FAIL line for the summary
        FAILING_STEP=$(grep "^  FAIL:" "$LOG_FILE" | head -1 || echo "unknown (exit $RC)")
        RESULT="FAIL: $FAILING_STEP"
        OVERALL_RC=1
    fi

    RESULTS+=("${IMAGE}: ${RESULT}")
    echo ""
    echo ">>> ${IMAGE}: ${RESULT}"
    echo "    Log: $LOG_FILE"
    echo ""
done

echo "============================================================"
echo "SUMMARY"
echo "============================================================"
for r in "${RESULTS[@]}"; do
    echo "  $r"
done
echo ""

exit $OVERALL_RC
