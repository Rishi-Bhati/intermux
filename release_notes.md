# InterMux Release Notes

## v2.0.1 — Linux fixes

* **Firefox profiles.** A symlinked Firefox profile root (for example one managed by a dotfile tool) no longer stops InterMux from reading the other roots, so Snap or Flatpak Firefox profiles are still found.
* **Security reports.** SECURITY.md sends every security and bug report to the Reportary portal. The bug report zip is created from the GUI's **Report issue** button or `intermux report`.
* **Internal.** Removed an unused field from the scroll helper in the GUI.

---

## v2.0.0 — Linux Overhaul

InterMux on Linux has been rewritten as a Python package. The window runs as your normal user,
and only a small helper, started through polkit, makes the network changes as root. Applications
still launch as you, with your profile and data.

### What's new

InterMux 2.0 is the biggest change since the first Linux release. It is safer, works with more
applications, and installs like any other program.

* **Route apps that are already running.** "Shift current session" moves a running application
  onto a chosen interface without restarting it. While it is routed, its traffic cannot leak out
  any other interface.
* **Assign many apps at once.** Assign sets up every selected application with a single
  authorization prompt.
* **Brave and other Chromium browsers.** Each interface gets its own browser profile, so a
  browser that is already open no longer blocks the new binding.
* **Name lookups inside each namespace.** Each namespace has its own DNS resolver, so name
  resolution does not depend on the host's `/etc/resolv.conf`.
* **Routing resets itself.** On systemd systems, a watcher tears down routing when a bound app
  exits, so the network is not left changed.
* **Privilege split.** The window and CLI run as you. Only a small helper, started through polkit,
  runs as root.
* **Bug reports stay on your disk.** The report dialog saves a zip you can attach to an issue.
  Nothing is uploaded automatically.
* **Pre-built bundle.** One download runs on glibc 2.35 and newer. Installing was tested on
  Ubuntu 22.04 and Debian 12.

### Features

* **Installable and runnable.** Install with `sudo ./install.sh` to get a system-wide program, a
  launcher entry and a polkit policy. Or run from source with `python3 -m intermux`.
* **Unprivileged GUI and CLI.** Listing interfaces, listing apps, status and the dependency check
  need no root. Changes ask for authorization once through the system's polkit prompt.
* **One network namespace per interface.** Each bound interface gets its own namespace, connected
  to the host with a veth pair, with its own routing table and rules. Traffic from a bound app
  always leaves through its interface.
* **Application picker.** Choose apps from the installed desktop applications, with search. Apps
  that cannot be bound are shown greyed out with the reason.
* **Public IP check.** Verify the public IP seen from any binding.
* **Safer cleanup.** Teardown idle removes plumbing for bindings with no running app. Reset
  everything removes all InterMux setup, and can also stop the apps it started.
* **Firefox profiles.** Use your default profile, or a separate profile per interface when Firefox
  is already running.
* **Logs per binding.** Each app's output goes to `~/.local/state/intermux/logs/`. The GUI can open
  it with one click.
* **Dependency check.** `intermux check` lists what is missing and how to install it.
* **Distribution.** Build with PyInstaller, uninstall with `sudo ./uninstall.sh`, and starting points
  for Arch, Debian and Fedora packaging are included (untested).

### Changes

* Python 3.9 or newer is required (was 3.6).
* There are no third-party Python dependencies.
* The old `core/`, `gui/` and root `cli.py` files are replaced by the `intermux` package.
* Logs moved from `~/.local/share/intermux/intermux.log` to per-binding files under
  `~/.local/state/intermux/logs/`.
* IPv4 forwarding is set through `/proc`. The `sysctl` tool (procps) is still used inside each namespace for the DNS redirect.

### Known limitations

* Flatpak apps cannot be bound yet.
* IPv4 only. Virtual and VPN interfaces are not offered for binding.

---

## v2.0.0 — Windows Proxy & App Launcher Fixes

### 🪟 Windows Platform Enhancements

* **SOCKS5 Proxy Environment**: Fixed an issue where the proxy environment variables were injecting the `http://` scheme instead of `socks5://`, causing applications to fail connection handshakes. All variables now strictly enforce `socks5://`.
* **Console Applications Support**: Console applications like `cmd.exe` and `powershell.exe` now properly spawn their own interactive windows when launched through the GUI or CLI, fixing failures when InterMux is compiled as a standalone `.exe`.
* **Fuzzy App Finder**: The advanced fuzzy finder for resolving application names without exact paths has been transferred to **Phase 2**. 
* **Performance Note**: Network speed over the proxy is currently a little slow. We are actively finding out ways to improve throughput and reduce proxy overhead in upcoming releases.

---
## v1.2.0 — Windows Support (Phase 1)

### 🪟 Windows Platform — Phase 1 (SOCKS5 Proxy Engine)

InterMux now runs on **Windows 10 and Windows 11**.

Because Windows has no equivalent to Linux network namespaces, a different approach is used:
a local **SOCKS5 proxy server** is started per interface (bound to that adapter's IP), and
the target application is launched with proxy environment variables injected. Traffic exits
through the correct physical NIC.

#### What's new

* **`windows/` directory**: Entirely separate from the Linux codebase — zero cross-sharing.
  * `windows/core/interface.py` — adapter detection via psutil + PowerShell fallback
  * `windows/core/proxy_engine.py` — asyncio SOCKS5 proxy server (TCP, Phase 1)
  * `windows/core/app_launcher.py` — app launcher with proxy env vars; Firefox profile + `user.js` auto-config; Chrome/Edge `--proxy-server` flag injection
  * `windows/core/platform_utils.py` — UAC, DNS, process detection, Firefox helpers
  * `windows/gui/app.py` — Tkinter GUI (same dark theme as Linux)
  * `windows/cli.py` — CLI with same command surface (`list`, `assign`, `check`, `clear`, `reset`)
* **Standalone `.exe`**: `windows/intermux_windows.spec` for building `InterMux.exe` via PyInstaller
* **Both distribution modes**: `.exe` for end users, Python project for developers
* **~1–2ms proxy overhead** per TCP connection — documented in the GUI and README
* **Zero additional dependencies**: only `psutil` required at runtime; Tkinter ships with Python

#### Known limitations (Phase 2 planned)

* UDP traffic (gaming, VoIP) not yet supported — SOCKS5 UDP relay coming in Phase 2
* Apps that ignore proxy env vars not yet supported — DLL-based socket binding coming in Phase 2

---

## v1.1.0

### 🚀 Features & Enhancements

* **Smart Interface Filtering**: The GUI dropdowns now automatically filter out virtual network interfaces (like `veth`, Docker bridges, `tun`/`tap`, and VM networks) to reduce clutter and prevent user confusion. You'll only see real physical or tethered interfaces.
* **Flawless Firefox Isolation**: Firefox integration has been completely overhauled. 
  * InterMux now always launches Firefox with a persistent, per-namespace profile (`intermux-<iface>`) and the `-new-instance --no-remote` flags.
  * This guarantees that namespaces never conflict with your main running Firefox instance, bypasses "Firefox is already running" lock errors entirely, and keeps your browsing data isolated per interface.
* **Robust Multi-Interface Routing (Policy Routing)**: Fixed a critical bug where assigning multiple interfaces (e.g., WLAN and USB tethering) would result in no internet access on some interfaces due to default routing table conflicts. InterMux now uses strict Policy Routing (with dedicated private routing tables and `ip rule` priorities per namespace) to guarantee that traffic from a namespace *always* exits via its assigned physical interface.

### 🛡️ Cross-Distro Hardening & Reliability

* **Persistent Comprehensive Logging**: InterMux now writes detailed debug logs, including full stack traces for errors, to `~/.local/share/intermux/intermux.log`. This makes cross-distro troubleshooting significantly easier.
* **Deterministic Subnet Generation**: Namespace subnets are now derived deterministically from an MD5 hash of the interface name, ensuring stable subnets across application restarts (fixing issues with Python's randomized `hash()`).
* **Smart Tool Path Auto-Detection**: Core system tools (`iptables`, `ip`, `sysctl`) are now auto-detected using `shutil.which` with sensible fallbacks (`/usr/sbin/`). It also intelligently detects `iptables-legacy` and `iptables-nft`, ensuring out-of-the-box compatibility across Arch, Ubuntu, Fedora, Alpine, openSUSE, and more.
* **Cleaner Reset & Teardown**: The `Reset Everything` feature has been upgraded to properly clean up all custom policy routing rules, private routing tables, and specific `iptables` NAT MASQUERADE rules added by InterMux. Harmless teardown errors (like "FIB table does not exist") are now silently logged as debug info rather than spamming the console as warnings.
