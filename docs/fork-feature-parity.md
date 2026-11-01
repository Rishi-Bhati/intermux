# Features of the Exordio fork, and where InterMux stands

[Exordio/intermux](https://github.com/Exordio/intermux) is a hard fork that started from InterMux 1.1.0
(release 2.1.1 at the time of this comparison). This list covers what it added on top of the old InterMux
and how each item maps onto this project, which has since been rewritten separately (2.0.x: unprivileged
GUI, polkit-gated helper, adoption of running apps, watcher service).

Status: **Added** = implemented on the `feature/ui-overhaul` branch; **Already here** = InterMux 2.0.x had it
before this work; **Different** = covered another way; **Not adopted** = left out on purpose.

## Networking

| Fork feature | Fork details | Status here |
|---|---|---|
| VPN interfaces | `tun*`, `tap*`, `wg*`, `ppp*` shown as VPN and bindable; route `dev <iface>` when there is no gateway | **Added.** Same prefixes; kind `vpn`; bindable without a main-table default route; the namespace route goes straight into the tunnel. |
| Unreachable fallback route | `unreachable default` at a high metric, so traffic never leaks onto the host route when the interface goes away | **Already here** (`routing.ensure_policy`). |
| Auto-reconnect | GUI reconnects `no route` namespaces on refresh; `reconnect` CLI command | **Already here** as the watcher service (`refresh_routes`, runs without the GUI). **Added:** `intermux reconnect` and Settings > Re-add routes. |
| Reserved routing tables avoided | Never uses tables 253-255 etc. | **Already here** (tables start at 1000). |
| Subnet / name collision handling | Hash of `<iface>#N` when a name or subnet is taken | **Different:** subnets come from a pool recorded in state, so collisions do not arise. |
| Hidden virtual interfaces | `veth*`, `docker*`, `br-*`, `virbr*`, `vboxnet*`, `vmnet*` | **Already here** (`VIRTUAL_PREFIXES`). |

## DNS

| Fork feature | Fork details | Status here |
|---|---|---|
| DNS per namespace | Auto, presets (Cloudflare, Google, Quad9) or a custom IPv4 list | **Added** (`dnsconf.py`, `bind --dns`, GUI DNS field). Up to 4 servers; loopback, link-local, multicast and unspecified rejected. |
| Change DNS of a live namespace | "Set DNS" / `dns` command; `resolv.conf` rewritten in place | **Added** (`set_dns` action, `intermux dns`, Settings > DNS servers). The DNS redirect also moves to the new servers, so running apps follow. |
| Auto DNS from `resolvectl` | Interface DNS, else system DNS minus other LANs' servers | **Different:** gateway first, then public resolvers, each probed from inside the namespace; stray DNS is redirected. This already avoided unreachable LAN resolvers. |
| Existing namespace keeps its DNS | Auto on an existing namespace leaves DNS alone | **Added** (`dns="keep"` default). |

## Traffic and bandwidth

| Fork feature | Fork details | Status here |
|---|---|---|
| Traffic per namespace | Totals and current rate from `ip -s link` of the host veth | **Added**, read from `/sys/class/net` without root: live rates and graphs per connection and per namespace, totals on app cards and in `intermux status`. |
| Bandwidth limits | `tc tbf`; download on the host veth, upload inside the namespace | **Added** (`system/shaping.py`, `set_limit`, `intermux limit`, Settings > Limit bandwidth). Limits are kept in state and re-applied on the next bind. |

## App and namespace control

| Fork feature | Fork details | Status here |
|---|---|---|
| Stop / Kill | SIGTERM then SIGKILL after 5 s; Kill sends SIGKILL at once | **Already here** (Stop, Force quit, `stop`, `kill --force`). |
| Restart | Stop and start again in the same namespace with the same options | **Added** (`restart_app`, `intermux restart`, Restart button). Keeps a separate Firefox profile. |
| Remove a namespace | Stop everything in it, remove it with its rules and routes | **Added** (`remove_namespace`, `intermux remove`, Settings > Remove namespace). Only the requesting user's processes are stopped; others keep it alive and are reported. |
| Move a namespace to another interface without stopping apps | Re-wire veth/NAT/route to the new interface; drop conntrack entries | **Different:** `move_app` (`intermux move`, More > Move to) restarts the app on the new connection. A live move would break the rule that a namespace's name, veth names and table derive from its interface. Running apps that InterMux did not start can already be moved live with "Shift current session". |
| Apps listed per namespace with PIDs, including ones not started by InterMux | Tree view in the Active table | **Added** in part: cards show the process count and PIDs of the namespace. |
| Apps survive closing InterMux | Launched in their own session, output to a log | **Already here.** |
| Find apps whose process name differs from the file | Symlink target name matched too | **Already here** (handlers' `comm_names`, namespace membership). |

## GUI

| Fork feature | Fork details | Status here |
|---|---|---|
| History of started apps | Last 20 in the App field, restores DNS | **Added** (`ui/history.py`, kept in `~/.local/state/intermux/history.json`); also restores the connection. |
| Pending preview | Namespace, subnet, new or existing, DNS | **Added** in part: queued apps say "new / existing namespace" and which DNS they get. |
| Connection states | connected / interface down / missing / no route | **Different:** lanes show routing, active, idle, saturated, no gateway and disconnected. |
| Window icon, polkit action with icon | | Polkit action and menu icon **already here**; **added** the window icon (`ui/appicon.py`). |
| GUI runs as root (re-exec with pkexec), `xhost` | | **Not adopted.** InterMux keeps the unprivileged GUI and the validated helper; this is a deliberate security choice. |
| Full desktop session environment for apps | `systemctl --user show-environment` | **Not adopted.** InterMux passes an allowlist of session variables; widening it is a security trade-off to decide separately. |

## Packaging

| Fork feature | Status here |
|---|---|
| `pyproject.toml`, Arch PKGBUILD, desktop entry, icons, polkit action, `install.sh` | **Already here** (plus Debian and Fedora starting points, PyInstaller bundle, one-line installers). |
