# intermuxd

The unprivileged, per-user InterMux 3.0 beta daemon. It reads kernel counters,
process membership and the helper's state file, and serves protocol v1 over a
Unix stream socket. The contract is [daemon-protocol.md](../docs/daemon-protocol.md).

Build and test on Linux with stable Rust (edition 2021):

```sh
cd rust
cargo build --release -p intermuxd
cargo test
cargo clippy --all-targets -- -D warnings
```

From the repository root, run the shared Python tests:

```sh
xvfb-run -a python3 -m unittest discover -s tests -t .
```

Run from `rust/` in a user session with `XDG_RUNTIME_DIR` set:

```sh
cargo run -p intermuxd -- --socket /tmp/x.sock
```

With no overrides the socket is `$XDG_RUNTIME_DIR/intermux/daemon.sock`. The
daemon creates its directory with mode 0700 and its socket with mode 0600.
`XDG_RUNTIME_DIR` is required, including when path overrides are supplied.
An existing listener is preserved; an unanswered stale socket is replaced.
`LISTEN_FDS=1` and `LISTEN_PID` equal to the daemon's PID select systemd's fd 3;
the daemon leaves an activated socket file in place on exit. Peers must have
the daemon's real UID, checked with Linux `SO_PEERCRED`.

Path overrides for tests:

```text
intermuxd [--socket PATH] [--state PATH] [--sys-class-net PATH] [--proc PATH] [--version]
```

Defaults are `/var/lib/intermux/state.json`, `/sys/class/net`, and `/proc`.
Namespace handles and adoption cgroups are read from `/var/run/netns` and
`/sys/fs/cgroup`. `--version` prints `intermuxd 3.0.0-beta.1` without requiring
a runtime directory. `ip` (iproute2) supplies interface snapshots; missing `ip`
is logged and the daemon continues with an empty interface list.
Logs go to stderr; `RUST_LOG=debug` changes verbosity.

Talk to the daemon from another terminal:

```sh
socat - UNIX-CONNECT:/tmp/x.sock
```

Enter one JSON object per line:

```json
{"v":1,"id":1,"method":"hello","params":{"client":"cli","protocols":[1]}}
{"v":1,"id":2,"method":"subscribe","params":{"topics":["interfaces","bindings","traffic"]}}
{"v":1,"id":3,"method":"interfaces.list","params":{}}
{"v":1,"id":4,"method":"bindings.list","params":{}}
{"v":1,"id":5,"method":"traffic.snapshot","params":{}}
{"v":1,"id":6,"method":"ping","params":{}}
```

Each connection has a bounded 64-message output queue; overflow disconnects
that client. Traffic is sampled once per second while traffic subscribers
exist. Interface changes use rtnetlink notifications and a 200 ms debounce;
netlink failure enables a 5 s rescan. State changes use a directory inotify
watch and a 200 ms debounce. Missing, partially written or invalid state leaves
the last good view intact and logs once until recovery. Lost state watches are
retried during the 2 s liveness checks. SIGTERM/SIGINT send `daemon.shutdown` to
every connected client, allow up to 1 s for queued output, and exit successfully.

Protocol interpretation where the prose/examples leave room for choice:

- `subscribe` replaces the connection's topics, preserving requested order,
  removing duplicates and ignoring unknown topics. An empty list unsubscribes.
- The 64 KiB limit counts bytes before the newline, including any carriage
  return. Only newline-terminated requests are processed.
- `interface.status` contains the complete Interface, as specified by the
  events table; the shorter envelope example illustrates event framing.
- Visibility uses the window's up-or-remembered filtering and stable ordering,
  with the contract's explicit inclusion of remembered virtual links. `lo` is
  excluded. Remembered links that vanish are represented as `missing`.
- Old bindings without UID use their live leader's real UID; when no owner
  can be determined, UID is null. Binding identity is interface, app and
  namespace; adoption identity is its ID. `app.exited` applies to a binding
  remaining in state whose namespace changes from nonempty to empty.
- `route.restored` follows a previously observed `route.lost` while routed apps
  remain. GUI raise events go to all GUI subscribers.

The shared inputs and expected classification/bindability answers live in
`tests/fixtures/` at the repository root. Both languages test against those
files. The daemon tests use fake `ip`, sysfs and proc trees; socket activation
is exercised through a small Python launcher. Runtime dependencies are Tokio,
Serde/serde_json, nix, inotify, netlink-sys/netlink-packet-route and
netlink-packet-core, log/env_logger, and md5. MD5 only reproduces Python's
stable interface-to-veth name mapping.

# intermux-tray

The per-user StatusNotifierItem tray (`ksni` over `zbus`; no GTK). Its menu is
the compact view: interfaces with state and speed, this user's apps with
Restart/Stop (adoptions: Release), speed-limit presets, Open InterMux, Start at
sign-in and Quit. It reads everything from the daemon and sends every change to
`pkexec /usr/lib/intermux/intermux helper` off the UI thread. It never runs as
root. `intermux-tray --version` prints the version; logs go to stderr
(`RUST_LOG`).

One instance per user (`flock` on `$XDG_RUNTIME_DIR/intermux/tray.lock`).
When no `org.kde.StatusNotifierWatcher` exists (stock GNOME) it sends one
notification and keeps running for notifications. It registers when a watcher
appears later. Idle uses SNI status `Active` rather than `Passive`, because some
hosts hide `Passive` items completely. Attention (a link with this user's apps
is not `connected`) uses `NeedsAttention` and an amber overlay dot.

Development overrides:

| Variable | Default |
|---|---|
| `INTERMUX_DAEMON_SOCKET` | `$XDG_RUNTIME_DIR/intermux/daemon.sock` |
| `INTERMUX_DAEMON` | `/usr/lib/intermux/intermuxd` (started if the socket does not answer) |
| `INTERMUX_HELPER` | `/usr/lib/intermux/intermux` (run through pkexec) |

Smoke test against a private daemon, without the real helper:

```sh
XDG_RUNTIME_DIR=/tmp/t target/release/intermuxd --socket /tmp/t/d.sock &
XDG_RUNTIME_DIR=/tmp/t INTERMUX_DAEMON_SOCKET=/tmp/t/d.sock INTERMUX_HELPER=/bin/false \
  target/release/intermux-tray
```
