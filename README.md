<div align="center">

# InterMux

### Advanced Network Interface Management for Linux

[![Python Version](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](https://opensource.org/license/mit)
[![Platform Linux](https://img.shields.io/badge/platform-Linux-orange.svg)](https://www.linux.org/)
[![Platform Windows](https://img.shields.io/badge/platform-Windows%2010%2F11-blue.svg)](windows/README.md)
[![Privilege Model](https://img.shields.io/badge/privileges-polkit%20helper-yellow.svg)](SECURITY.md)

<p align="center">
  <strong>Bind applications to specific network interfaces with ease</strong>
</p>

[Features](#features) • [Installation](#installation) • [Usage](#usage) • [Architecture](#architecture) • [Security](SECURITY.md) • [Contributing](#contributing)

</div>

---

## Overview

**InterMux** is a Linux utility that binds applications to specific network interfaces. Whether you want your browser on Wi-Fi while your development server stays on Ethernet, or you want to isolate an application for security testing, InterMux makes it simple.

> **Privacy Note:** InterMux sets up network plumbing as root through a small helper, but **launches all applications as your regular user**. Your bookmarks, cookies, and app settings are always preserved.

### Use Cases

- **Multi-WAN Management**: Route different applications through different internet connections
- **Network Testing**: Test applications on specific network interfaces
- **Security Isolation**: Isolate applications in their own network namespaces
- **Bandwidth Management**: Control which apps use which network connections
- **Development**: Test network-dependent applications across different interfaces

## Why I Made It

I was downloading a large file via torrent, and while waiting, I opened YouTube to pass the time.
But the videos were stuck on low quality, buffering like crazy, even though my connection showed 300 Mbps.

Turns out, the torrent was consuming all the bandwidth, and YouTube was left starving.
That's when I had the idea: "What if I could assign different apps to different networks?"

After some research, I found that while Linux supports advanced networking, there wasn't a simple tool to do what I needed, especially one that was GUI-friendly and straightforward.

So I built InterMux: a utility that lets me bind any app to a specific interface with ease. No messy configs, no guesswork, just full control.

## Features

<table>
<tr>
<td>

### Core Features
- **Auto-detection** of all network interfaces
- **Per-interface routing tables** and policy rules
- **Network namespace isolation** for each bound app
- **Live interface list** that refreshes automatically
- **Both CLI and GUI** interfaces
- **User-preserving**: apps keep your profile and data
- **Public IP check** for each binding
- **Desktop app picker** built from installed applications

</td>
<td>

### Interface Support
- **Wi-Fi** (wlan*, wl*)
- **Ethernet** (eth*, en*)
- **USB tethering**
- **Bluetooth tethering**
- **Mobile broadband** (wwan*)

Virtual bridges (Docker, veth, libvirt, VMware, VirtualBox) and tun/tap devices are hidden from the picker. Other tunnels, such as WireGuard and Tailscale, can appear in it.

</td>
</tr>
</table>

## Installation

There are two ways to run InterMux. Option A installs a standalone program system-wide. Option B runs the Python source directly.

### Option A — Install as a System App (Easiest) ⭐

This builds a standalone Linux binary and installs it system-wide. The program goes to `/usr/lib/intermux/`, `/usr/bin/intermux` links to it, and a polkit policy, a `.desktop` entry, an icon and AppStream metadata are installed so InterMux appears in your application launcher.

```bash
# Clone the repository
git clone https://github.com/Rishi-Bhati/intermux.git
cd intermux

# Run the installer as root
sudo ./install.sh
```

> **What it does:** `install.sh` builds the binary as your user (not as root) in a temporary virtual environment with a pinned PyInstaller, then copies it into place. Nothing is built inside the repository. It checks for `ip`, `iptables`, `runuser` and `pkexec` (and `pgrep`, which is optional), and prints install hints for anything missing.
>
> If you already have a build, you can skip the build step. Download the tarball from the **Build InterMux on Linux** workflow in the GitHub Actions tab, extract it so that `dist/intermux/` exists, and then run `sudo ./install.sh`.

To remove InterMux, run `sudo ./uninstall.sh`. It first runs `intermux reset --kill-apps --yes`, so every binding is torn down and the state is deleted.

### Option B — Run as Python Project (No Build Needed)

**Prerequisites:** Python 3.9+, Git, and the networking tools below. InterMux has no third-party Python dependencies, so there is no `pip install` step and no virtual environment is required. The GUI needs Tk, which is `python3-tk` on most distributions.

<details>
<summary><strong>Ubuntu / Debian / Mint</strong></summary>

```bash
sudo apt update
sudo apt install -y python3 python3-tk iproute2 iptables policykit-1 procps util-linux curl
```
</details>

<details>
<summary><strong>Arch / EndeavourOS / Manjaro</strong></summary>

```bash
sudo pacman -S python tk iproute2 iptables polkit procps-ng util-linux curl
```
</details>

<details>
<summary><strong>Fedora / RHEL / Rocky</strong></summary>

```bash
sudo dnf install -y python3 python3-tkinter iproute iptables polkit procps-ng util-linux curl
```
</details>

<details>
<summary><strong>openSUSE</strong></summary>

```bash
sudo zypper install python3 python3-tk iproute2 iptables polkit procps util-linux curl
```
</details>

- Ubuntu 20.04 ships Python 3.8, which is too old. Install a newer Python 3 first.
- `curl` is optional, but `intermux verify` needs it.

#### Clone and Run

```bash
# Clone the repository
git clone https://github.com/Rishi-Bhati/intermux.git
cd intermux

# Check dependencies (no root needed)
python3 -m intermux check

# Start the GUI
python3 -m intermux
```

When you run from source, the polkit policy is attached to the installed binary, so the authorization prompt will be generic and refer to the Python interpreter. Option A gives the InterMux-specific prompt.

## Usage

### GUI Mode (Recommended)

```bash
intermux              # installed (Option A)
python3 -m intermux   # from source (Option B)
```

The GUI runs as you. When a change is needed, it asks for authorization through polkit. Apps always launch as **your regular user**, so your profile data is preserved.

<details>
<summary><strong>GUI Features</strong></summary>

- **Interface dropdown**: Only interfaces that can be bound (up, IPv4 address, default route, not virtual). Refreshes every few seconds.
- **Application field**: Type a name (found on your `PATH`) or a path. **Browse...** picks a file; **Pick...** opens a searchable list of installed desktop applications. Entries that cannot be bound are greyed out with the reason.
- **Open separate profile if already running**: Checkbox that decides what happens when the app is already open.
- **Bindings table**: Shows app, interface, status, and public IP. Select a row to **Verify** (show the public IP), **Stop**, or **Open log**.
- **Bottom buttons**: **Teardown idle** removes plumbing for bindings with no running app. **Reset everything** removes all InterMux setup and can also stop the apps. **Force-quit app** kills every instance of an app.
- **Report issue**: Opens a small dialog with two actions. **Save bug report** writes a zip with the app logs and system details to `~/intermux-reports/`, with personal data scrubbed on a best-effort basis. **Open reporting portal** opens the web page where you upload the zip and describe the problem.
- **Keyboard**: Enter binds, Delete stops the selected binding, F5 refreshes.
- Errors appear in dialogs and in the status bar. A dark theme is used by default, and a light theme is used when GNOME reports a light colour scheme.

</details>

### CLI Mode

Run the CLI as your normal user. It asks for authorization only when a change is needed.

| Command | What it does | Root needed |
|---------|--------------|-------------|
| `intermux interfaces [--all]` | List bindable interfaces (`--all` shows every interface) | No |
| `intermux apps [--search TEXT]` | List installed desktop applications | No |
| `intermux bind --app APP --iface IF [--separate-profile \| --close-existing] [--allow-unsupported]` | Bind an application to an interface | Yes (polkit prompt) |
| `intermux status` | List each binding with its namespace and NAT state | No |
| `intermux stop --app APP --iface IF` | Stop the application bound to an interface | Yes |
| `intermux kill --app APP [--force]` | Stop every instance of an application on the host | Yes |
| `intermux verify --iface IF` | Show the public IP seen from an interface | Yes |
| `intermux teardown-idle` | Remove plumbing for bindings with no running app | Yes |
| `intermux reset [--kill-apps] [--yes]` | Remove everything InterMux created | Yes |
| `intermux check` | Dependency report | No |
| `intermux report [--web] [--out DIR]` | Save a bug report zip (default `~/intermux-reports/`, or `DIR`); `--web` opens the reporting portal instead | No |

`APP` can be a command name (`firefox`), an absolute path, or a path starting with `~`.

```bash
intermux interfaces
intermux bind --app firefox --iface wlan0
intermux verify --iface wlan0
intermux status
intermux stop --app firefox --iface wlan0
intermux reset --kill-apps --yes
```

If the application is already running, `bind` asks what to do:

```
[!] 'firefox' is already running.
    [1] close & reopen  (uses your default profile)
    [2] separate profile
    [3] cancel
```

`reset` without `--yes` asks you to type `yes` to continue.

From source, replace `intermux` with `python3 -m intermux`.

### Tutorial Video

<video controls width="100%">
  <source src="https://res.cloudinary.com/dzsghc33d/video/upload/v1752167379/Screencast_20250710_223520_gfnqkq.webm" type="video/webm">
  Your browser does not support the video tag.
</video>

![Screencast_20250710_223520_gfnqkq](https://github.com/user-attachments/assets/c018bddd-7f32-4471-a3f1-da75d4463c3c)

## Architecture

```
intermux/                        Python package (standard library only)
├── __main__.py                  entry point: GUI, helper, or CLI
├── cli.py                       command-line interface
├── config.py                    paths, subnet pool, environment allowlist
├── errors.py                    exception types with stable error codes
├── report.py                    reporting portal address and browser launch
├── bugreport.py                 bug report zip: system details and scrubbed logs
├── subnets.py                   10.200.N.0/24 pool allocation
├── state.py                     state file (/var/lib/intermux/state.json) and lock
├── engine.py                    bind, stop, verify and teardown orchestration
├── system/                      wrappers around ip, iptables, /proc and sysfs
│   ├── commands.py              safe command runner (argument lists only)
│   ├── procs.py                 process discovery and termination
│   ├── session.py               user and display/session detection
│   ├── deps.py                  distro detection and dependency checks
│   ├── netinfo.py               interface discovery and classification
│   ├── netns.py                 namespaces and veth pairs
│   ├── dns.py                   upstream DNS servers
│   ├── routing.py               policy rules and routing tables
│   └── firewall.py              NAT rules and IP forwarding
├── apps/                        application handling
│   ├── __init__.py              path resolution and per-app handler selection
│   ├── firefox.py               Firefox profile handling
│   └── desktop_entries.py       .desktop application discovery
├── helper/                      privileged side and unprivileged client
│   ├── protocol.py              request validation (pure)
│   ├── server.py                root helper, started through pkexec
│   ├── client.py                unprivileged caller
│   └── __main__.py              python3 -m intermux.helper
└── ui/                          Tk GUI
    ├── app.py                   main window
    ├── worker.py                background calls so the window never blocks
    ├── picker.py                desktop application picker
    ├── report_dialog.py         Report issue dialog
    └── theme.py                 dark and light palettes
packaging/                       polkit policy, desktop file, icon, metainfo, distro packaging
tests/                           unit tests and opt-in namespace integration test
windows/                         Windows 10/11 edition (separate codebase)
install.sh                       build and install system-wide
uninstall.sh                     remove the installed files
intermux_linux.spec              PyInstaller spec (onedir build)
pyproject.toml                   package metadata and the intermux command
```

### How It Works

1. **Interface discovery**: Interface state comes from `ip -j` JSON output. The interface kind (Wi-Fi, Ethernet, USB, Bluetooth, mobile) comes from `/sys/class/net`.
2. **One namespace per interface**: Each bound interface gets its own network namespace, connected to the host by a veth pair. The host side uses `10.200.N.1/24` and the namespace side uses `10.200.N.2/24`, where N is a free index from 1 to 254.
3. **Policy routing**: Traffic from each subnet uses its own routing table (`1000 + N`), whose default route goes out through the chosen interface's gateway. Traffic from a namespace always leaves through its assigned interface.
4. **NAT and forwarding**: iptables MASQUERADE rules are added for the subnet, with matching FORWARD rules. IPv4 forwarding is turned on through `/proc`, and the previous value is restored when the last binding is removed.
5. **DNS**: Each namespace gets its own `/etc/netns/<namespace>/resolv.conf`: the bound interface's gateway first (the router or phone on that link), then the public resolvers `1.1.1.1` (Cloudflare) and `9.9.9.9` (Quad9). The host's resolvers are not used, because some of them (such as `systemd-resolved` at `127.0.0.53` or Tailscale's `100.100.100.100`) are not reachable through the bound interface. Lookups that fall back to the public resolvers are seen by those providers.
6. **Launch**: The application starts inside its namespace, then drops to your user with `runuser` and a minimal environment. Only display, audio, D-Bus, and locale variables are passed through.
7. **State**: Bindings are recorded in `/var/lib/intermux/state.json` under a lock, so the GUI, the CLI and the helper always agree.
8. **Automatic cleanup**: The `intermux-watch` systemd service (installed and started by `install.sh`) checks every 3 seconds. When the last app in a namespace exits, it removes that binding and, once no other app uses the interface, its namespace, routes and NAT rules. It also re-adds a policy route that the kernel dropped after the interface went down and came back. It never signals applications. Without systemd, run `intermux teardown-idle` yourself.

### Privilege Model

| Operation | Runs As |
|-----------|---------|
| List interfaces, list apps, status, check | **you** (no root) |
| Create namespaces, veth pairs, routes, NAT, forwarding | **root helper** (started via `pkexec`, polkit action `org.intermux.run`) |
| Stop or kill applications | **root helper** (signals the processes) |
| Launch your application | **your regular user** (via `runuser` inside the namespace) |

The GUI and CLI never run root commands themselves. Every change goes through the helper, which checks each request against a strict schema before acting on it. See [SECURITY.md](SECURITY.md) for details.

## Troubleshooting

<details>
<summary><strong>GUI does not start, or says there is no display</strong></summary>

The GUI needs a graphical session. Run it from your desktop, not from a plain text console. Check that `DISPLAY` or `WAYLAND_DISPLAY` is set, and that Tk is installed (`python3 -c "import tkinter"`).

On Wayland, Tk runs through XWayland. If it fails, make sure XWayland is installed:

```bash
# Arch
sudo pacman -S xorg-xwayland
# Ubuntu/Debian
sudo apt install xwayland
```
</details>

<details>
<summary><strong>"authorization was cancelled or denied"</strong></summary>

The polkit prompt was dismissed or timed out. Run the command again and approve it. If no prompt appears, make sure a polkit authentication agent is running in your session, and that `pkexec` is installed.

For Option A, check that the policy is installed:

```bash
ls /usr/share/polkit-1/actions/org.intermux.policy
```
</details>

<details>
<summary><strong>Interface is missing from the list</strong></summary>

Only interfaces that are up, have an IPv4 address, have a default route, and are not virtual can be bound. Virtual bridges (Docker, veth, libvirt, VMware, VirtualBox) and `tun`/`tap` devices are hidden. Other tunnels, such as WireGuard and Tailscale, can appear in the list.

```bash
intermux interfaces --all
ip -br addr show
ip route show default
```
</details>

<details>
<summary><strong>No internet in the namespace, or DNS failures</strong></summary>

```bash
intermux check
intermux verify --iface wlan0
cat /proc/sys/net/ipv4/ip_forward   # should print 1 while bindings exist
```

If `iptables` is missing, `check` reports it. If you see leftover rules after a crash, run `intermux teardown-idle`, or `intermux reset --kill-apps --yes` to clean up everything.
</details>

<details>
<summary><strong>Stale or unreadable state</strong></summary>

If the state file cannot be read, InterMux moves it aside as `/var/lib/intermux/state.json.corrupt-<timestamp>` and starts with an empty state. Existing namespaces and rules can then be removed with `intermux reset --yes`. The corrupt file is kept, so you can inspect it.
</details>

### Logs

Each bound application writes its output to its own log file:

```bash
~/.local/state/intermux/logs/<app>-<iface>.log
```

The **Open log** button in the GUI opens this file. Include the relevant log when reporting issues.

## Known Limitations

### Browser Compatibility

- **Firefox**: Supported. Uses your existing profile, or a separate profile per interface if you choose that. The separate profile is always created under `~/.mozilla/firefox/intermux-<iface>`. Finding your default profile covers native, Snap and Flatpak installs, but a Snap or Flatpak Firefox may not be able to read the separate profile because of its sandbox.
- **Chromium-based browsers** (Chromium, Chrome, Brave, Vivaldi, Edge, Opera): Supported. They are single-instance, so if the browser is already running you are asked to close it or open a separate profile. A separate profile uses its own data folder per browser and interface, under `~/.local/state/intermux/profiles/<browser>-<iface>`.
- **Flatpak apps**: Cannot be bound. They are reported as not bindable, because they run in their own sandbox.
- **Other apps**: Command-line and GUI applications generally work.

### Networking

- **IPv4 only**. Interfaces need an IPv4 address and a default route.
- **Up to 254 bound interfaces**, one `10.200.N.0/24` subnet each.
- **All protocols follow the interface**: Routing works at the network layer, so TCP and UDP traffic from a bound app both use the chosen interface.
- **Virtual bridges and `tun`/`tap` devices** are not offered for binding. Other tunnels, such as WireGuard and Tailscale, can appear and can be bound if they meet the requirements above.

### Platform Support

- **Linux**: Requires a Linux kernel with `CONFIG_NET_NS` (network namespaces), Python 3.9+, and polkit.
- **X11 and Wayland**: Both session types are supported for the GUI.
- **Windows 10 / 11**: Supported through a SOCKS5 proxy binding. It is a separate codebase. See [windows/README.md](windows/README.md).

### System Requirements

- Root privileges for network operations, granted through polkit
- `iproute2`, `iptables` (or `iptables-nft`/`iptables-legacy`), `util-linux` (`runuser`), and `polkit` (`pkexec`)
- Optional: `procps` (`pgrep`), and `xdg-utils` (`xdg-open`) to open folders and links

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for the development setup, test commands and code rules.

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/AmazingFeature`)
3. Commit your changes
4. Push and open a Pull Request

To report a security issue, follow [SECURITY.md](SECURITY.md) and do not open a public issue.

## License

This project is licensed under the MIT License. See the [LICENSE](license) file for details.

## Acknowledgments

- Built with Python and Tkinter
- Uses the Linux networking stack (iproute2, netlink, network namespaces)
- Inspired by the need for better network interface management

## Support

- [Report bugs](https://github.com/Rishi-Bhati/intermux/issues)
- [Request features](https://github.com/Rishi-Bhati/intermux/issues)

---

## Windows Support

InterMux supports Windows 10 and 11 through a SOCKS5 proxy engine.
See **[windows/README.md](windows/README.md)** for installation and usage instructions.

```
windows/
├── gui/app.py              GUI (same dark theme)
├── cli.py                  CLI (same commands)
├── core/                   Windows-specific engine
└── intermux_windows.spec   PyInstaller build to .exe
```

---

<div align="center">
  <strong>Made with ❤️ for the Linux and Windows communities</strong>
</div>
