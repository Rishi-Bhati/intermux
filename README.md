<div align="center">

# InterMux

**Bind individual desktop applications to specific network interfaces on Linux.**

[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](license)
[![Platform: Linux](https://img.shields.io/badge/platform-Linux-orange.svg)](#requirements)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/downloads/)
[![Status: beta](https://img.shields.io/badge/status-beta-yellow.svg)](#limitations)

</div>

InterMux lets you choose which network connection each application uses. For example, you can keep a torrent client on Ethernet while your browser uses Wi-Fi or a phone hotspot. It runs each bound application inside its own network namespace and routes that namespace through the interface you pick. A small root helper, gated by polkit, sets up the networking. Your applications always run as your own user, so your profiles and settings are kept.

InterMux has a graphical interface and a command line. Version 2.0.0 is the Linux edition. A separate Windows edition lives in [`windows/`](windows/README.md).

## Supported platforms

| Platform | Support | Documentation |
|----------|---------|---------------|
| Linux (kernel with network namespaces, systemd) | This edition. Network namespaces, policy routing and a polkit-gated helper. | This README |
| Windows 10 / 11 | Separate code base using a local SOCKS5 proxy per interface. | [windows/README.md](windows/README.md) |

## What it does

- **Bind an application to an interface.** Pick an interface and an application. The application starts with all its traffic going through that interface.
- **Route a running app.** If the application is already open, choose "Shift current session" instead of restarting it. InterMux moves the running copy onto the other interface without closing it.
- **See the status.** The GUI and `intermux status` show every binding and every routed running app.
- **Verify the exit IP.** InterMux asks a public IP service which address it sees from inside a given interface.
- **Clean up automatically.** When the last process of a bound application exits, a background watcher removes the plumbing for that binding.

## Features

- Per-application binding through one network namespace per interface (veth pair, NAT, policy routing).
- DNS inside each namespace is kept on that namespace's own resolvers, even if the host rewrites `/etc/resolv.conf`.
- Routing of an already running application through a cgroup v2 group and nftables packet marks, without restarting it.
- Desktop application list read from `.desktop` entries (system, user, Flatpak and Snap export directories), plus free-form names or paths.
- Special handling for Firefox and for the Chromium family (Chromium, Chrome, Brave, Vivaldi, Edge, Opera) to open a separate profile or close and reopen the browser.
- GUI with Pending and Active binding lists, per-binding Verify, Stop, Log and Force quit actions.
- Full command line for scripting (`bind`, `status`, `stop`, `kill`, `verify`, `adopt`, `release`, `teardown-idle`, `reset`, `interfaces`, `apps`, `check`, `report`).
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
| `xdg-open` | xdg-utils; opening links and folders | Optional |

Run `intermux check` to see what is missing on your system.

### Package names by distribution

Names are taken from the files in `packaging/` and from the hints printed by `install.sh` and `intermux check`.

| Distribution | Runtime packages | Optional | Extra for building from source |
|--------------|------------------|----------|--------------------------------|
| Arch | `iproute2 iptables polkit util-linux procps-ng xdg-utils` | `nftables curl` | `tk` (and `python-pyinstaller` for the PKGBUILD) |
| Debian / Ubuntu | `iproute2 iptables pkexec` (or `policykit-1`) `util-linux procps xdg-utils` | `nftables curl` | `python3-tk python3-venv` |
| Fedora | `iproute iptables-nft polkit util-linux procps-ng xdg-utils` | `nftables curl` | `python3-tkinter` |
| openSUSE | `iproute2 iptables procps util-linux polkit xdg-utils` | `nftables curl` | `python3-tk` |

There is no openSUSE package recipe in `packaging/`; the openSUSE names come from `install.sh`.

## Installation

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
| Arch | `packaging/arch/PKGBUILD` | The maintainer field is a TODO and `sha256sums` is `SKIP`; set the real checksum (`updpkgsums`) first. |
| Debian / Ubuntu | `packaging/debian/` | Standard `debian/` directory (control, rules, postinst). |
| Fedora | `packaging/fedora/intermux.spec` | Needs `python3-pyinstaller` at build time, so it is only buildable from a COPR or private repository that provides it. |

### Uninstall

```bash
sudo ./uninstall.sh          # from the source or release directory
sudo ./uninstall.sh --force  # remove files even if the reset step fails
```

The script stops the watcher, resets all bindings and stops the running bound apps, removes the installed files, and removes state and runtime data. With `--force` the files are removed even if the reset fails, which can leave namespaces and NAT rules behind.

## Usage

### GUI

Start InterMux from your application menu, or run `intermux` in a terminal (no command opens the GUI). Do not run it as root.

| Element | What it does |
|---------|--------------|
| **Interface** | Choose the network interface the application should use. |
| **Application** | Choose an installed application ("Choose application"), or type a name or path. |
| **Add** | Add the interface and application pair to the Pending list. |
| **Assign** | Apply every pending binding. This is the step that asks for your password. |
| **Bindings** | Two lists. Pending holds entries that are not applied yet. Active holds running bindings and routed (adopted) apps. |
| **Verify** | On an active binding, shows the public IP seen through that interface. |
| **Stop** | Stops the application bound to that interface. |
| **Log** | Opens the log of that binding. |
| **Force quit** | Force-stops the bound application. |
| **Release** | On a routed (adopted) app, stops routing it. |
| **Teardown idle** | Removes the plumbing of bindings that have no running application. |
| **Reset everything** | Removes everything InterMux created. It asks whether to also stop the applications inside InterMux namespaces. |
| **Report issue** | Opens the bug report dialog (see [Troubleshooting](#troubleshooting)). |

When you press Assign for an application that is already running, InterMux asks what to do: close and reopen it, open a separate profile, or "Shift current session". Shift routes the running copy through the interface without restarting it. The option "Open a separate profile if the app is already running" in the form sets that choice in advance.

### Command line

Run `intermux --help` or `intermux <command> --help` for details. The CLI refuses to run as root.

| Command | Purpose | Example |
|---------|---------|---------|
| `interfaces [--all]` | List network interfaces (`--all` includes ones that cannot be bound). | `intermux interfaces` |
| `apps [--search TEXT]` | List desktop applications. | `intermux apps --search fire` |
| `bind --app APP --iface IF [--separate-profile \| --close-existing]` | Run an application inside a namespace bound to an interface. | `intermux bind --app firefox --iface wlan0` |
| `status` | Show bindings. | `intermux status` |
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

Network changes need root. InterMux asks polkit (`pkexec`) to run its helper, and polkit shows a password dialog. The prompt appears once per Assign batch, not once per application. The policy requires administrator authentication each time; the authorization is not cached.

## How it works

1. **Namespace per interface.** Each bound interface gets its own network namespace (`ns_<8 hex>`) connected to the host by a veth pair on a `10.200.N.0/24` subnet.
2. **NAT and policy routing.** The host forwards and masquerades the namespace's traffic out of the chosen interface. A dedicated routing table and `ip rule` entry send that subnet's traffic through that interface.
3. **DNS in each namespace.** Each namespace has its own `resolv.conf`: the interface's gateway first, then public resolvers. IPv4 DNS sent to any other address is also redirected to the namespace's resolvers, so a host that rewrites `/etc/resolv.conf` does not break it.
4. **Root helper through polkit.** The GUI and CLI send one JSON request to `intermux helper`, started with `pkexec`. The helper makes the network changes and starts the application as you with `runuser`.
5. **Watcher.** `intermux-watch.service` runs as root under systemd. It removes bindings whose namespace has no processes left and re-adds routes that the kernel dropped (for example after a link flap).
6. **Routing a running app.** InterMux moves the application's processes into a cgroup v2 group. An nftables rule marks packets from that cgroup, a `fwmark` policy rule sends marked packets to a per-interface routing table, and they are masqueraded on that interface. IPv6 for the group is blackholed so it cannot leak around the chosen interface.

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
- **Routing a running app drops existing TCP connections.** The application keeps running and reconnects on its own.
- **DNS in a routed running app.** DNS lookups from an app routed with "Shift current session" may still use the default route instead of the chosen interface.
- **Shared public IP.** Two interfaces can still show the same public IP when they use the same upstream connection.
- **cgroup v2 is required** to route a running app (InterMux reports `cgroup v2 is required to route a running app` otherwise). Routing a running app also needs `nft`.
- **glibc 2.35 or newer** is needed for the pre-built bundle.
- **Status.** This is beta software and it changes system networking. Try it on a machine where you can recover from a mistake.

## Troubleshooting

- **Logs.** Each binding writes a log under `~/.local/state/intermux/logs`. The GUI "Log" button opens it.
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
  ui/          Tk GUI (app, picker, theme, worker, report dialog)
packaging/
  linux/       polkit policy, desktop entry, icon, metainfo, systemd unit and preset
  arch/ debian/ fedora/   distribution packaging starting points
tests/         unit tests and an optional root integration test
windows/       separate Windows edition
install.sh, uninstall.sh, intermux_linux.spec
SECURITY.md, CONTRIBUTING.md, CHANGELOG.md, license
```

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md). Run the unit tests with:

```bash
python3 -m unittest discover -s tests -t . -v
```

The namespace integration test changes networking on the machine it runs on, so it needs root and is skipped by default. Run it only in a virtual machine or other disposable system; see CONTRIBUTING.md for the command.

To report a security problem, follow [SECURITY.md](SECURITY.md) and do not open a public issue.

## License

MIT. See the [license](license) file.
