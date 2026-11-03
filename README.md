<div align="center">

# InterMux

**Bind individual desktop applications to specific network interfaces on Linux.**

[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](license)
[![Platform: Linux](https://img.shields.io/badge/platform-Linux-orange.svg)](#requirements)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/downloads/)
[![Status: beta](https://img.shields.io/badge/status-beta-yellow.svg)](#limitations)

</div>

InterMux lets you choose which network connection each application uses. For example, you can keep a torrent client on Ethernet while your browser uses Wi-Fi or a phone hotspot. It runs each bound application inside its own network namespace and routes that namespace through the interface you pick. A small root helper, gated by polkit, sets up the networking. Your applications always run as your own user, so your profiles and settings are kept.

InterMux has a graphical interface and a command line. Version 2.1.0 is the Linux edition. A separate Windows edition lives in [`windows/`](windows/README.md).

## Supported platforms

| Platform | Support | Documentation |
|----------|---------|---------------|
| Linux (kernel with network namespaces, systemd) | This edition. Network namespaces, policy routing and a polkit-gated helper. | This README |
| Windows 10 / 11 | Separate code base using a local SOCKS5 proxy per interface. | [windows/README.md](windows/README.md) |

## What it does

- **Bind an application to an interface.** Pick an interface and an application in the New binding form, then start it. The application starts with all its traffic going through that interface.
- **Route a running app.** If the application is already open, choose "Shift current session" in the prompt instead of restarting it. InterMux moves the running copy onto the chosen interface without closing it.
- **See every connection and app.** The Bindings table has one row per interface, with the apps routed through it listed under it. Each row shows its status, PID, download and upload speed, a graph of the last minute, its speed limit, DNS and public IP.
- **See the status.** The Bindings table and `intermux status` show every binding and every routed running app. `intermux status` also shows the DNS, bandwidth limit and traffic of each interface namespace.
- **Tune each connection.** For an interface, set its DNS servers, set a speed limit, or remove its namespace with everything in it. For an app, restart it, move it to another interface, or stop it.
- **Verify the exit IP.** InterMux asks a public IP service which address it sees from inside a given interface.
- **Clean up automatically.** When the last process of a bound application exits, a background watcher removes the plumbing for that binding.

## Features

- Per-application binding through one network namespace per interface (veth pair, NAT, policy routing).
- VPN tunnels (`tun`, `tap`, `wg`, `ppp`) can be bound: one app can use the VPN while the rest of the system does not. VPN connections can be connected, disconnected and imported from the window (NetworkManager).
- DNS inside each namespace is kept on that namespace's own resolvers, even if the host rewrites `/etc/resolv.conf`. The resolvers are picked automatically, from a preset (Cloudflare, Google, Quad9), or typed in with Custom… (up to four IPv4 servers). They can be changed while apps run.
- Bandwidth limits per interface namespace (download and upload, Mbit/s) with `tc`.
- Live traffic rates, read from the kernel's interface counters without root: per interface, and per namespace for the apps InterMux started on it. Shifted apps are counted in their interface's figure.
- Routing of an already running application through a cgroup v2 group and nftables packet marks, without restarting it.
- Desktop application list read from `.desktop` entries (system, user, Flatpak and Snap export directories), plus free-form names or paths.
- Special handling for Firefox and for the Chromium family (Chromium, Chrome, Brave, Vivaldi, Edge, Opera) to open a separate profile or close and reopen the browser.
- GUI with a New binding form, a queue that starts many apps with one password prompt, and a Bindings table that shows each interface with its apps, their traffic and their limits. Its buttons follow the selection. Recently routed apps are remembered with their DNS.
- Full command line for scripting (`bind`, `status`, `stop`, `kill`, `restart`, `move`, `verify`, `dns`, `limit`, `remove`, `reconnect`, `adopt`, `release`, `teardown-idle`, `reset`, `interfaces`, `apps`, `check`, `report`).
- Dependency check (`intermux check`) with a package-manager-specific install hint for each missing tool.
- Bug report zip written to your own disk; nothing is uploaded automatically.
- Privilege split: the GUI and CLI never run root commands themselves. See [Security model](#security-model).

## Requirements

- Linux with network namespaces and `systemd` (the routing watcher is a systemd service).
- cgroup v2, only needed to route an already running app.
- Python 3.9 or newer with `tkinter` for the GUI (only needed when you build from source; the release bundle includes its own).
- Tools:

| Tool | Package role | Required |
|------|--------------|----------|
| `ip` | iproute2 | Yes |
| `iptables` (or iptables-nft) | NAT for namespaces | Yes |
| `sysctl` | procps; DNS redirect inside namespaces | Yes |
| `runuser` | util-linux; launches apps as your user | Yes |
| `pkexec` | polkit; privilege prompt | Yes |
| `nft` | nftables; routing a running app | Optional |
| `curl` | public IP verification | Optional |
| `tc` | iproute2 (`iproute-tc` on Fedora); bandwidth limits | Optional |
| `xdg-open` | xdg-utils; opening links and folders | Optional |

Run `intermux check` to see what is missing on your system.

### Package names by distribution

Names are taken from the files in `packaging/` and from the hints printed by `install.sh` and `intermux check`.

| Distribution | Runtime packages | Optional | Extra for building from source |
|--------------|------------------|----------|--------------------------------|
| Arch | `iproute2 iptables polkit util-linux procps-ng xdg-utils` | `nftables curl` | `tk` (and `pyinstaller` from the AUR for the PKGBUILD) |
| Debian / Ubuntu | `iproute2 iptables pkexec` (or `policykit-1`) `util-linux procps xdg-utils python3-tk` | `nftables curl` | `python3-tk python3-venv` |
| Fedora | `iproute iptables-nft polkit util-linux procps-ng xdg-utils hicolor-icon-theme` | `nftables curl` | `python3-tkinter` |
| openSUSE | `iproute2 iptables procps util-linux polkit xdg-utils` | `nftables curl` | `python3-tk` |

There is no openSUSE package recipe in `packaging/`; the openSUSE names come from `install.sh`.

## Installation

### Quick install

```bash
curl -fsSL https://raw.githubusercontent.com/Rishi-Bhati/intermux/main/get/install.sh | sh
```

Run it as your normal user; it asks for your password through `sudo`. It installs any missing system tools with your package manager, downloads the newest release, checks its SHA-256 and installs it. On systems the pre-built bundle does not support (aarch64, glibc older than 2.35, musl) it builds from the tagged source instead. Run the same command again to update. Options go after `sh -s --`:

```bash
curl -fsSL https://raw.githubusercontent.com/Rishi-Bhati/intermux/main/get/install.sh | sh -s -- --uninstall
curl -fsSL https://raw.githubusercontent.com/Rishi-Bhati/intermux/main/get/install.sh | sh -s -- --version 2.0.1
```

`--from-source` forces a source build and `--no-deps` skips installing system packages. Without curl, use `wget -qO- <same URL> | sh`. See [`get/README.md`](get/README.md) for details, including the Windows installer.

The steps below do the same thing by hand.

The bundle is installed system-wide under `/usr/lib/intermux`, with a launcher at `/usr/bin/intermux`, a polkit policy, a desktop entry and the `intermux-watch.service` systemd unit.

### Pre-built release

Download `intermux-linux-x86_64.tar.gz` and its `.sha256` file from the [GitHub releases](https://github.com/Rishi-Bhati/intermux/releases), check the checksum, then extract it as root so the files are root-owned and run the installer:

```bash
sha256sum -c intermux-linux-x86_64.tar.gz.sha256
sudo tar -xzf intermux-linux-x86_64.tar.gz
cd <extracted directory>
sudo ./install.sh
```

The installed bundle runs as root through polkit, so `install.sh` refuses a bundle that a normal user can write to. It also checks the bundle against `dist/intermux.sha256`. The `--allow-local-bundle` and `--bundle-sha256 HEX` options exist for reviewed local bundles.

The bundle is built on Ubuntu 22.04 and needs **glibc 2.35 or newer** (Ubuntu 22.04+, Debian 12+, Fedora 36+, Arch). It does not run on Debian 11, Ubuntu 20.04, RHEL/Rocky/Alma 8 or 9, or musl systems such as Alpine. On those, install from source.

### From source

```bash
git clone https://github.com/Rishi-Bhati/intermux.git
cd intermux
sudo ./install.sh
```

Without a `dist/` directory, `install.sh` builds the PyInstaller bundle as your own user (`$SUDO_USER`) in a temporary virtualenv, copies the result to a root-owned staging directory, and then installs it. It needs `python3`, the `venv` module and `tkinter`. If it cannot find a needed tool it prints an install command for your distribution. Re-running the script is safe.

### Distribution packages

Recipes exist in the repository, but they are starting points that have not been published to any repository. Build them on the target distribution.

| Distribution | Location | Notes |
|--------------|----------|-------|
| Arch | `packaging/arch/PKGBUILD` | Builds with `makepkg`; needs `pyinstaller` from the AUR. Not published to the AUR yet. Update `sha256sums` (`updpkgsums`) for each release. |
| Debian / Ubuntu | `packaging/debian/` | Standard `debian/` directory (control, rules, postinst). |
| Fedora | `packaging/fedora/intermux.spec` | Needs `python3-pyinstaller` at build time, so it is only buildable from a COPR or private repository that provides it. |

### Uninstall

```bash
curl -fsSL https://raw.githubusercontent.com/Rishi-Bhati/intermux/main/get/install.sh | sh -s -- --uninstall
sudo ./uninstall.sh          # from the source or release directory
sudo ./uninstall.sh --force  # remove files even if the reset step fails
```

The script stops the watcher, resets all bindings and stops the running bound apps, removes the installed files, and removes state and runtime data. With `--force` the files are removed even if the reset fails, which can leave namespaces and NAT rules behind.

## Usage

### GUI

Start InterMux from your application menu, or run `intermux` in a terminal (no command opens the GUI). Do not run it as root.

The window has three sections: **New binding**, **Queue** and **Bindings**. Drag the gaps between them to resize them.

| Element | What it does |
|---------|--------------|
| **New binding** | Choose the **Interface**, then the **Application**: type a name or path, pick a recent one, or use **Choose…** to pick from the installed applications. **DNS** offers Automatic, Cloudflare, Google, Quad9 and **Custom…**, which lets you type up to four IPv4 servers. **Speed limit** sets the download and upload speed in Mbit/s. **Add to queue** puts the application in the queue. |
| **Queue** | The applications waiting to start, with their interface, DNS, speed limit and what to do if the application is already running. **Start all** starts the whole queue and asks for your password once. **Remove** and **Clear** change the list. |
| **Bindings** | One row per interface, with the apps routed through it listed under it. Columns: Status, PID, Download, Upload, a "Last minute" graph, Speed limit, DNS and Public IP. |
| **Buttons under Bindings** | They follow the selected row. For an app: **Check public IP**, **Restart**, **Stop**, **Open log**, **Move to…** and **Force quit**. For a shifted app: **Release**. For an interface: **DNS servers…**, **Speed limit…** and **Remove namespace…**, and **Check public IP** while one of its apps runs. Right-click a row for the same actions. |
| **VPN connections…** | Connect or disconnect NetworkManager VPN connections, or import a WireGuard (`.conf`) or OpenVPN (`.ovpn`) file. With "Only for apps I route through it" ticked (the default), a VPN connected here only carries the apps you route through it; the rest of the system keeps its normal route. |
| **Refresh** | Reads the state again now. |
| **Report issue** | Opens the bug report dialog (see [Troubleshooting](#troubleshooting)). |
| **Re-add routes** | Re-adds the routes of interfaces that are back, as `intermux reconnect` does. |
| **Remove stopped** | Removes the plumbing of bindings that have no running application. |
| **Reset everything** | Removes everything InterMux created. It asks whether to also close the applications running through InterMux. |

Tick "Open a separate profile if the app is already running" in the New binding form to start a second copy with its own profile instead of being asked. Otherwise, when you press **Start all** for an application that is already running, InterMux asks what to do: close and reopen it, open a separate profile, **Shift current session**, or cancel. Shift routes the running copy through the interface without restarting it.

### Command line

Run `intermux --help` or `intermux <command> --help` for details. The CLI refuses to run as root.

| Command | Purpose | Example |
|---------|---------|---------|
| `interfaces [--all]` | List network interfaces (`--all` includes ones that cannot be bound). | `intermux interfaces` |
| `apps [--search TEXT]` | List desktop applications. | `intermux apps --search fire` |
| `bind --app APP --iface IF [--separate-profile \| --close-existing] [--dns SERVERS]` | Run an application inside a namespace bound to an interface, optionally with chosen DNS (`auto`, `cloudflare`, `google`, `quad9` or addresses). | `intermux bind --app firefox --iface wlan0 --dns quad9` |
| `status` | Show bindings, and the DNS, bandwidth limit and traffic of each interface namespace. | `intermux status` |
| `restart --app APP --iface IF` | Restart an application in its namespace with the same profile. | `intermux restart --app firefox --iface wlan0` |
| `move --app APP --iface IF --to IF` | Move a bound application to another interface (it restarts there). | `intermux move --app firefox --iface wlan0 --to wg0` |
| `dns --iface IF SERVERS` | Change the DNS servers of an interface namespace; running apps pick it up. | `intermux dns --iface wlan0 1.1.1.1,9.9.9.9` |
| `limit --iface IF [--down MBIT] [--up MBIT]` | Limit the bandwidth of an interface namespace; without values the limit is removed. | `intermux limit --iface wlan0 --down 20 --up 5` |
| `remove --iface IF [--yes]` | Stop everything in an interface namespace and remove it. | `intermux remove --iface wlan0` |
| `reconnect` | Re-add the routes of namespaces whose interface is back (the watcher also does this on its own). | `intermux reconnect` |
| `stop --app APP --iface IF` | Stop the application bound to an interface. | `intermux stop --app firefox --iface wlan0` |
| `kill --app APP [--force]` | Stop every host instance of an application (`--force` sends SIGKILL to survivors). | `intermux kill --app firefox` |
| `verify --iface IF` | Print the public IP seen from an interface namespace. | `intermux verify --iface wlan0` |
| `adopt --app APP --iface IF` | Route an already running application through an interface. | `intermux adopt --app firefox --iface eth0` |
| `release ID` | Stop routing an adopted application. The ID is shown by `adopt` and `status`. | `intermux release <ID>` |
| `teardown-idle` | Remove plumbing of bindings with no running applications. | `intermux teardown-idle` |
| `reset [--kill-apps] [--yes]` | Remove everything InterMux created. | `intermux reset --kill-apps --yes` |
| `check` | Report missing dependencies. | `intermux check` |
| `report [--web] [--out DIR]` | Save a bug report zip (default folder `~/intermux-reports`), or open the reporting portal with `--web`. | `intermux report` |

`helper` and `watch` also exist, but they are internal commands used by the installed services and are not meant for direct use.

### The password prompt

Network changes need root. InterMux asks polkit (`pkexec`) to run its helper, and polkit shows a password dialog. The prompt appears once per **Start all** batch, not once per application. The policy requires administrator authentication each time; the authorization is not cached.

## How it works

1. **Namespace per interface.** Each bound interface gets its own network namespace (`ns_<8 hex>`) connected to the host by a veth pair on a `10.200.N.0/24` subnet.
2. **NAT and policy routing.** The host forwards and masquerades the namespace's traffic out of the chosen interface. A dedicated routing table and `ip rule` entry send that subnet's traffic through that interface. When firewalld is running, the host end of the namespace's link is also added to its trusted zone while the namespace exists (runtime only).
3. **DNS in each namespace.** Each namespace has its own `resolv.conf`: the servers you chose, or by default the interface's gateway first, then public resolvers. IPv4 DNS sent to any other address is also redirected to the namespace's resolvers, so a host that rewrites `/etc/resolv.conf` does not break it.
4. **Root helper through polkit.** The GUI and CLI send one JSON request to `intermux helper`, started with `pkexec`. The helper makes the network changes and starts the application as you with `runuser`.
5. **Watcher.** `intermux-watch.service` runs as root under systemd. It removes bindings whose namespace has no processes left and re-adds routes that the kernel dropped (for example after a link flap).
6. **Bandwidth limits.** A token bucket filter (`tc qdisc ... tbf`) shapes the host end of the veth pair for download and the namespace end for upload, so a limit covers every app on that interface together.
7. **VPN tunnels.** A tunnel such as WireGuard has no gateway, so the namespace's default route goes straight into it (`dev wg0`). The unreachable fallback keeps the app offline, rather than on the normal route, while the tunnel is down.
8. **Traffic counters.** The GUI reads `/sys/class/net/*/statistics` once a second, as your user. A namespace's traffic is read from the host end of its veth pair, which everything it sends or receives passes through.
9. **Routing a running app.** InterMux moves the application's processes into a cgroup v2 group. An nftables rule marks packets from that cgroup, a `fwmark` policy rule sends marked packets to a per-interface routing table, and they are masqueraded on that interface. IPv6 for the group is rejected, so it cannot leak around the chosen interface and apps fall back to IPv4. DNS (port 53) is not marked, so name lookups use the normal route.

State is kept in `/var/lib/intermux/state.json`.

## Security model

The full policy, including how to report a vulnerability, is in [SECURITY.md](SECURITY.md). In summary:

- The GUI and CLI run as your user and never run root commands themselves.
- Network changes are made by a helper started with `pkexec`. Polkit action `org.intermux.run` requires administrator authentication for every use.
- As root, the binary refuses every command except `helper` and `watch`, and refuses `watch` when started through `pkexec`.
- The helper checks each request: JSON of at most 1 MiB, known keys and actions only, a strict interface-name pattern, absolute application paths, and an allowlist of environment variables.
- No shell is used. Every external command is an argument list.
- Applications run as the invoking user (`runuser -u <you>` with a clean environment), never as root.
- A namespace isolates routing, not the application. A bound app can still read your files, so do not bind software you do not trust.

## Limitations

- **Flatpak apps cannot be bound.** They run in their own sandbox; InterMux marks them as not bindable.
- **Snap and Flatpak Firefox.** Opening a separate Firefox profile uses a profile directory that a Snap- or Flatpak-confined Firefox may not be able to read.
- **Connections opened before a shift keep their old route.** "Shift current session" applies to connections the app opens afterwards; ones it already had open stay on the old route until they reconnect. Reload a page or reopen a tab to move it.
- **DNS in a routed running app.** Name lookups of an app routed with "Shift current session" use the system's normal route, not the chosen interface, so resolvers that only another link reaches keep working. The app's other traffic uses the chosen interface.
- **Traffic is per interface namespace.** All apps bound to one interface share one namespace, so their DNS and bandwidth limit are shared too, and the speed on an app row is the namespace's speed. A shifted app has no counter of its own; its traffic is part of its interface's speed.
- **Moving an app restarts it.** A namespace's routing belongs to its interface, so InterMux moves a bound app by starting it again on the new connection. To move an app without restarting it, use "Shift current session".
- **Shared public IP.** Two interfaces can still show the same public IP when they use the same upstream connection.
- **cgroup v2 is required** to route a running app (InterMux reports `cgroup v2 is required to route a running app` otherwise). Routing a running app also needs `nft`.
- **glibc 2.35 or newer** is needed for the pre-built bundle.
- **Status.** This is beta software and it changes system networking. Try it on a machine where you can recover from a mistake.

## Troubleshooting

- **Logs.** Each binding writes a log under `~/.local/state/intermux/logs`. The **Open log** button opens it.
- **Bug report.** Use the **Report issue** button in the GUI, or run `intermux report`. This writes a zip to `~/intermux-reports` (change with `--out DIR`). Nothing is uploaded; you attach the zip yourself. `intermux report --web` opens the reporting portal.
- **Debug output.** Run the CLI with `INTERMUX_DEBUG=1`, for example `INTERMUX_DEBUG=1 intermux status`, to send debug logging to stderr.
- **Missing tools.** Run `intermux check`. The GUI also shows a banner when a required tool is missing.
- **Watcher not running.** Applications still work, but idle bindings are not cleaned up automatically. Check it with:

  ```bash
  sudo systemctl status intermux-watch.service
  sudo systemctl enable --now intermux-watch.service
  ```

  On a system without systemd the watcher is not started by `install.sh`. You can run `intermux teardown-idle` by hand.
- **Something is stuck.** Press **Reset everything** in the GUI, or run `intermux reset` (add `--kill-apps` to also stop the bound applications). `sudo ./uninstall.sh` also resets before removing files.

## Project layout

```
intermux/
  cli.py, engine.py, config.py, state.py   command line, orchestration, constants, state file
  watch.py, report.py, bugreport.py        watcher, report data, bug report zip
  apps/        desktop entries, Firefox and Chromium handling, app path resolution
  helper/      protocol, root helper server, unprivileged client
  system/      namespaces, firewall, routing, running-app adoption, deps, safe file operations
  dnsconf.py   DNS presets and validation
  ui/          Tk GUI (main window, dialogs, graphs, picker, history, VPN and report dialogs, theme, worker)
get/           one-line installers (install.sh for Linux, install.ps1 for Windows)
packaging/
  linux/       polkit policy, desktop entry, icon, metainfo, systemd unit and preset
  arch/ debian/ fedora/   distribution packaging starting points
tests/         unit tests and an optional root integration test
windows/       separate Windows edition
install.sh, uninstall.sh, intermux_linux.spec
SECURITY.md, CONTRIBUTING.md, CHANGELOG.md, license
docs/          notes, such as the feature comparison with the Exordio fork
```

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md). Run the unit tests with:

```bash
python3 -m unittest discover -s tests -t . -v
```

The namespace integration test changes networking on the machine it runs on, so it needs root and is skipped by default. Run it only in a virtual machine or other disposable system; see CONTRIBUTING.md for the command.

To report a security problem, follow [SECURITY.md](SECURITY.md) and do not open a public issue.

## Acknowledgements

Some features in 2.1.0 were inspired by Exordio's fork, [Exordio/intermux](https://github.com/Exordio/intermux).

## License

MIT. See the [license](license) file.
