# InterMux Rune desktop UI

Build the preview with Rust 1.100 and the Linux Wayland and XKB development libraries. The separate Cargo workspace keeps Rune and its pinned patches out of the daemon and tray builds.

```sh
cargo +1.100.0 build --locked --manifest-path rust/intermux-ui/Cargo.toml
INTERMUX_GUI=rune python3 -m intermux
```

The packaged launcher starts the same native window. Its unprivileged Python child serves newline-delimited JSON over private stdin and stdout pipes. Read-only discovery, routing mutations, and live snapshots run independently, so an authorization prompt does not pause traffic updates. Mutations go through the existing polkit helper. The child also listens to the existing daemon for window activation and state-change notifications. The interface continues to work with local snapshots when the daemon is unavailable.

## Recreate the website captures

The named scenes use the same views as a live launch and read synthetic values. They start neither the bridge nor the daemon.

```sh
cargo +1.100.0 build --locked --manifest-path rust/intermux-ui/Cargo.toml
for scene in dashboard composer vpn; do
    rust/intermux-ui/target/debug/intermux-ui --scene "$scene" --size 1240x775 --scale 2 --snap "/tmp/intermux-$scene.png"
    ffmpeg -loglevel error -y -i "/tmp/intermux-$scene.png" -c:v libwebp -lossless 1 "website/public/app-preview/$scene.webp"
done
```

The embedded Schibsted Grotesk and JetBrains Mono fonts come from the [Google Fonts repository at revision 06bfe0d](https://github.com/google/fonts/tree/06bfe0d62a98375cb09c942fa18e68bd32cff057/ofl). Their SIL Open Font Licenses are in `fonts/` and ship with the Linux bundle.
