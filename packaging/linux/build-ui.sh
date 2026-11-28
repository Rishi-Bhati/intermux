#!/bin/sh
# Build into a separate Cargo target and copy the binary into a Linux onedir bundle.
set -eu

bundle=$1
target=$2

if command -v rustup >/dev/null 2>&1 && rustup run 1.100.0 cargo --version >/dev/null 2>&1; then
    set -- rustup run 1.100.0 cargo
else
    set -- cargo
fi
"$@" build --release --locked --manifest-path rust/intermux-ui/Cargo.toml --target-dir "$target"
install -m 0755 "$target/release/intermux-ui" "$bundle/intermux-ui"
for license in rust/intermux-ui/fonts/*-OFL.txt; do
    install -Dm644 "$license" "$bundle/licenses/${license##*/}"
done
