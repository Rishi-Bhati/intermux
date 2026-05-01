# InterMux Release Notes

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
