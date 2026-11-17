#!/bin/sh
# Runs inside ubuntu:22.04. /src = clean git archive of the release commit (read-only), /out = output.
set -eu
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null
apt-get install -y -qq python3 python3-venv python3-tk libpython3.10 binutils xvfb xauth curl build-essential pkg-config >/dev/null
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
. "$HOME/.cargo/env"
rm -rf /job && mkdir -p /job && cp -a /src/. /job/tree && cd /job/tree
python3 -m venv /job/venv
/job/venv/bin/pip install -q --upgrade pip "pyinstaller==6.20.0"
xvfb-run -a /job/venv/bin/python -m unittest discover -s tests -t . 2>&1 | tail -3
PYINSTALLER_CONFIG_DIR=/job/pyi /job/venv/bin/pyinstaller intermux_linux.spec --noconfirm \
    --distpath /job/dist --workpath /job/work >/job/pyi.log 2>&1 || { tail -30 /job/pyi.log; exit 1; }
test -x /job/dist/intermux/intermux
CARGO_TARGET_DIR=/job/cargo-target cargo build --release --locked --manifest-path rust/Cargo.toml
for bin in intermuxd intermux-tray; do
    if [ -f "/job/cargo-target/release/$bin" ]; then
        echo "Copying /job/cargo-target/release/$bin to /job/dist/intermux/"
        cp "/job/cargo-target/release/$bin" "/job/dist/intermux/$bin"
        chmod 0755 "/job/dist/intermux/$bin"
    fi
done
STAGE=/job/intermux-linux
mkdir -p "$STAGE/dist" "$STAGE/packaging"
cp -a /job/dist/intermux "$STAGE/dist/intermux"
cp -a packaging/linux "$STAGE/packaging/linux"
cp install.sh uninstall.sh README.md license "$STAGE/"
cp packaging/linux/README-install.txt "$STAGE/README-install.txt"
(cd "$STAGE/dist/intermux" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1) > "$STAGE/dist/intermux.sha256"
cd /job && tar --owner=0 --group=0 --numeric-owner -czf /out/intermux-linux-x86_64.tar.gz intermux-linux
cd /out && sha256sum intermux-linux-x86_64.tar.gz > intermux-linux-x86_64.tar.gz.sha256
"$STAGE/dist/intermux/intermux" --version 2>&1 || true
ls -la /out
