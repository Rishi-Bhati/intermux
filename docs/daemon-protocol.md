# InterMux daemon protocol, version 1

`intermuxd` is an unprivileged per-user daemon (Rust, `rust/intermuxd`). It watches interfaces, bindings,
adoptions and traffic, and tells clients (the tray, the window, the CLI) what changed. It never changes
anything and never calls the root helper: every change still goes from a client, inside the user's session,
to `pkexec /usr/lib/intermux/intermux helper`.

This file is the contract between the daemon (Rust) and its clients (Rust tray, Python window and CLI).
Change it before changing either side, and bump `protocol` for incompatible changes.

## Transport

- Unix stream socket `$XDG_RUNTIME_DIR/intermux/daemon.sock`. The directory is created with mode 0700, the
  socket with 0600. The daemon checks `SO_PEERCRED` and closes connections whose uid is not its own.
- Started by systemd (`intermuxd.socket`, socket activation: the daemon uses an inherited fd when
  `LISTEN_FDS=1` and `LISTEN_PID` is its pid) or by a client that finds no socket (it runs
  `/usr/lib/intermux/intermuxd` detached, then retries for up to 3 s).
- Framing: one JSON object per line (UTF-8, `\n`). A line longer than 64 KiB closes the connection.
- Every object has `"v": 1`.

## Messages

Request (client → daemon):
```json
{"v": 1, "id": 7, "method": "bindings.list", "params": {}}
```
Response (daemon → client), same `id`:
```json
{"v": 1, "id": 7, "result": {...}}
{"v": 1, "id": 7, "error": {"code": "unknown_method", "message": "no method named foo"}}
```
Event (daemon → client, only after `subscribe`, no `id`):
```json
{"v": 1, "event": "interface.status", "data": {"interface": "wlan0", "state": "connected"}}
```
Error codes: `bad_request`, `unknown_method`, `unsupported_version`, `internal`.

## Methods

| Method | Params | Result |
|---|---|---|
| `hello` | `{"client": "tray" \| "gui" \| "cli" \| str, "protocols": [1]}` | `{"daemon_version": "3.0.0-beta.1", "protocol": 1, "capabilities": ["interfaces", "bindings", "traffic", "gui"]}`. Must be the first request; others before it get `bad_request`. If no listed protocol is supported: error `unsupported_version`. |
| `subscribe` | `{"topics": ["interfaces", "bindings", "traffic", "gui"]}` | `{"subscribed": [...]}` (unknown topics are ignored and left out) |
| `interfaces.list` | `{}` | `{"interfaces": [Interface]}` |
| `bindings.list` | `{}` | `{"bindings": [Binding], "adoptions": [Adoption]}` |
| `traffic.snapshot` | `{}` | the last `traffic.sample` data, or `{"t": 0, "interfaces": {}, "namespaces": {}}` |
| `gui.raise` | `{}` | `{"raised": true}` if a client subscribed to `gui` exists (the daemon sends it a `gui.raise` event), else `{"raised": false}` (the caller should start the window itself) |
| `ping` | `{}` | `{"pong": true}` |

### Interface
```json
{"name": "wlan0", "kind": "wifi", "up": true, "bindable": true, "ipv4": ["192.168.1.20/24"],
 "gateway": "192.168.1.1", "state": "connected", "routed": 2, "link_speed": 108000000.0,
 "dns": [], "limit": {"down": 20.0, "up": null}}
```
- `kind`: `wifi`, `ethernet`, `usb`, `bluetooth`, `mobile`, `vpn`, `virtual`, `other` (same rules as
  `intermux/system/netinfo.py: classify_kind`; shared test fixtures in `tests/fixtures/`).
- `bindable`: up, has IPv4, and has a default route through it or is a VPN tunnel (`netinfo.Interface.bindable`).
- `state`: `connected` (up and bindable), `no_route` (up, not bindable), `down` (exists, not up),
  `missing` (has bindings/adoptions in state but no such link).
- `routed`: number of live bound apps + adoptions on it.
- `link_speed`: bytes/s from `/sys/class/net/<if>/speed`, or null.
- `dns`: servers chosen by hand for this interface's namespace (state.json `dns[iface]`); `[]` means Automatic.
- `limit`: state.json `limits[iface]` in Mbit/s (`{"down": float|null, "up": float|null}`), or null when none.
- Interfaces listed: the same set the window shows (physical and VPN links, plus any link in state), no `lo`,
  no virtual links unless they are bound.

### Binding and Adoption
Taken from `/var/lib/intermux/state.json` (world-readable; written atomically by the helper), plus liveness:
```json
{"iface": "wlan0", "app": "/usr/bin/firefox", "app_name": "firefox", "namespace": "ns_1a2b3c4d",
 "pid": 4242, "uid": 1000, "running": true, "pids": [4242, 4250],
 "log_path": "/home/u/.local/state/intermux/logs/firefox-wlan0.log"}
{"id": "a87f9e9dfa6450ab", "iface": "wlan0", "app": "/usr/bin/sleep", "app_name": "sleep", "uid": 1000,
 "running": true, "pids": [192]}
```
`running`/`pids`: processes currently in the binding's namespace (or the adoption's cgroup). Bindings of every
user are listed (state.json is world-readable anyway); clients show only their own uid's by default.

## Events

| Topic | Event | Data |
|---|---|---|
| interfaces | `interface.added`, `interface.removed` | Interface (removed: `{"name"}`) |
| interfaces | `interface.status` | Interface (sent when `up`, `state`, `bindable`, `ipv4`, `gateway` or `routed` changes) |
| interfaces | `route.lost`, `route.restored` | `{"interface": "wg0"}` — a link with routed apps goes `connected` → not, and back |
| bindings | `binding.started`, `binding.stopped` | Binding |
| bindings | `adoption.started`, `adoption.stopped` | Adoption |
| bindings | `app.exited` | `{"iface", "app_name", "uid", "expected": false}` — a bound app's last process ended while its binding is still in state (crash or user closed it) |
| traffic | `traffic.sample` | once per second: `{"t": 1763452800.5, "interfaces": {"wlan0": {"down": 12345.0, "up": 678.0}}, "namespaces": {"wlan0": {"down": 1000.0, "up": 50.0}}}` — bytes/s; `namespaces` is per bound interface, from the host-side veth (download = veth tx, upload = veth rx, like `traffic.veth_sample`) |
| gui | `gui.raise` | `{}` |
| (all) | `daemon.shutdown` | `{}` — sent to every client before the daemon exits |

## Sources and timing

- Interfaces: `ip -j link`, `ip -j addr`, `ip -j route show table main` on start and whenever an rtnetlink
  multicast message arrives (`RTMGRP_LINK | RTMGRP_IPV4_IFADDR | RTMGRP_IPV4_ROUTE`, debounced 200 ms).
  Falls back to a 5 s rescan if the netlink socket cannot be opened.
- State: inotify on `/var/lib/intermux/` (the helper replaces `state.json` atomically), debounced 200 ms.
- Liveness: `/proc/<pid>/ns/net` inode vs. the namespace inode (`/var/run/netns/<ns>`), and
  `/sys/fs/cgroup/<adoption group>/cgroup.procs`, rechecked every 2 s.
- Traffic: `/sys/class/net/<if>/statistics/{rx,tx}_bytes` every 1 s, only while a client subscribes to `traffic`.
- Restart: the daemon keeps no state of its own; it rebuilds everything on start. Clients reconnect with
  backoff (0.5 s, 1 s, 2 s, then every 5 s).
