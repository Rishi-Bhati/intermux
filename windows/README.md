<div align="center">

# 🌐 InterMux for Windows

### Advanced Network Interface Management for Windows

[![Python Version](https://img.shields.io/badge/python-3.8%2B-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](../license)
[![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-blue.svg)](https://www.microsoft.com/windows)
[![Phase](https://img.shields.io/badge/phase-1%20(TCP%20proxy)-orange.svg)](#how-it-works)

<p align="center">
  <strong>Bind applications to specific network interfaces with ease — on Windows</strong>
</p>

</div>

---

## 📋 Overview

**InterMux for Windows** lets you assign specific applications to specific network adapters — so Firefox can use your Wi-Fi while your download client uses Ethernet, for example.

> **How it works on Windows:** Unlike Linux (which uses kernel network namespaces), Windows requires a different approach. InterMux starts a **local SOCKS5 proxy** bound to your chosen adapter's IP address, then launches the app with proxy environment variables set. Traffic flows through the proxy and exits via the correct adapter.

> **Transparency note:** The proxy adds roughly **1–2 ms** of latency per TCP connection. This is imperceptible for browsing, streaming, and file downloads. The DLL-based approach (zero latency) is planned for Phase 2.

---

## ✨ What Works in Phase 1

| Application type | Works? | Notes |
|---|---|---|
| **Firefox** | ✅ Full support | Proxy prefs auto-written to profile |
| **Chrome / Edge / Brave** | ✅ | `--proxy-server` flag injected automatically |
| **curl, wget, aria2** | ✅ | Respect `ALL_PROXY` env var |
| **Most download managers** | ✅ | Honor HTTP_PROXY / SOCKS5 |
| **Steam, Discord** | ✅ | Honor proxy env vars |
| **Apps that ignore proxy settings** | ⚠️ Partial | Phase 2 will add DLL-based binding |
| **UDP-based traffic (games, VoIP)** | 🔜 Phase 2 | SOCKS5 UDP relay planned |

---

## 🚀 Getting the .exe

### Option A — Download from Pre-release (Easiest) ⭐

The standalone `.exe` is available for download in the GitHub Releases section under the latest pre-release tag.

1. Go to your repo on GitHub → **Releases**
2. Click the latest pre-release version
3. Scroll down to **Assets** → download **InterMux.exe**
4. Double-click `InterMux.exe` to run (approve UAC if prompted)

> **Why can't I just run `pyinstaller` on Linux?**  
> PyInstaller cannot cross-compile. Running it on Linux produces a Linux ELF binary that Windows cannot run. The spec file now blocks this with a clear error message. The build **must happen on a Windows machine**.

---

### Option B — Build Locally on a Windows Machine

If you have access to a Windows 10/11 machine:

```powershell
# Clone the repo on Windows
git clone https://github.com/Rishi-Bhati/intermux.git
cd intermux

# Install deps
pip install pyinstaller psutil

# Build the exe (must be run on Windows)
pyinstaller windows\intermux_windows.spec

# Output: dist\InterMux.exe
```

---

### Option C — Run as Python Project (No Build Needed)

**Prerequisites:**
- Python 3.8+ ([python.org](https://www.python.org/downloads/))
- Git

```powershell
# Clone the repo
git clone https://github.com/Rishi-Bhati/intermux.git
cd intermux

# Create a virtual environment (recommended)
python -m venv venv
venv\Scripts\activate

# Install Windows dependencies
pip install -r windows\requirements.txt
```

---

## 📖 Usage

### 🎨 GUI Mode (Recommended)

```powershell
# From the repo root (as Administrator):
python windows\gui\app.py
```

Or just double-click **InterMux.exe** (it requests UAC automatically).

**GUI Flow:**
1. Select your target network interface from the dropdown (e.g., `Wi-Fi`, `Ethernet`)
2. Enter the application path or name (e.g., `firefox`, `C:\Program Files\...`)
3. Click **+ Add to Queue**
4. Click **⚡ Assign All** → app launches bound to the chosen adapter

### 💻 CLI Mode

```powershell
# List active interfaces
python windows\cli.py list

# Assign Firefox to Wi-Fi
python windows\cli.py assign --app firefox --iface "Wi-Fi"

# Assign a full path
python windows\cli.py assign --app "C:\Program Files\MyApp\app.exe" --iface "Ethernet"

# Check dependencies
python windows\cli.py check

# Stop all proxies
python windows\cli.py clear

# Full reset
python windows\cli.py reset
```

---

## 🏗️ Architecture

```
windows/
├── cli.py                   # CLI entrypoint
├── requirements.txt         # Python dependencies (psutil only)
├── intermux_windows.spec    # PyInstaller build spec
├── core/
│   ├── interface.py         # Adapter detection (psutil + PowerShell)
│   ├── proxy_engine.py      # Asyncio SOCKS5 proxy server
│   ├── app_launcher.py      # Launch apps with proxy env vars
│   └── platform_utils.py   # UAC, DNS, process detection, Firefox helpers
└── gui/
    └── app.py               # Tkinter dark-theme GUI
```

### 🔧 How It Works

```
Your App
   ↓ (SOCKS5 via 127.0.0.1:<port>)
InterMux Proxy Server
   ↓ (TCP socket bound to chosen adapter IP)
Network Adapter (Wi-Fi / Ethernet / USB)
   ↓
Internet
```

1. **Detect adapters**: Uses `psutil` (or PowerShell fallback) to enumerate physical adapters
2. **Start proxy**: Spins up an `asyncio` SOCKS5 server on `127.0.0.1:<random_port>`
3. **Bind outgoing**: Every connection through the proxy is opened with `local_addr=(adapter_ip, 0)` — forcing it through the right NIC
4. **Inject env vars**: App is launched with `ALL_PROXY`, `HTTP_PROXY`, `HTTPS_PROXY` set to the local proxy
5. **Firefox extra**: Writes `user.js` proxy prefs directly into the Firefox profile for guaranteed enforcement

---

## 🛠️ Administrator Privileges

InterMux for Windows **does not require admin** for basic proxy-based binding (Phase 1). However, running as Administrator is recommended because:
- Some adapters restrict socket binding to elevated processes
- UAC elevation is auto-requested by the `.exe` via the manifest

If you see binding errors, right-click and **Run as administrator**.

---

## 🐛 Troubleshooting

<details>
<summary><strong>App doesn't seem to use the chosen interface</strong></summary>

- The app may not respect proxy environment variables. Check Phase 2 (DLL binding) for broader app support.
- For Firefox/Chrome, InterMux writes proxy settings directly into the profile — confirm the correct profile is being loaded.
- Run `python windows\cli.py list` to verify the interface has an IP address.

</details>

<details>
<summary><strong>Proxy connection refused / app can't connect</strong></summary>

```powershell
# Check that psutil is installed
python -c "import psutil; print('OK')"

# Check the interface has an IP
python windows\cli.py list

# Try the CLI directly to see debug output
python windows\cli.py assign --app firefox --iface "Wi-Fi"
```

</details>

<details>
<summary><strong>Interface not listed</strong></summary>

- Virtual adapters (Hyper-V, VPN TAP, Loopback) are hidden by design.
- Ensure the adapter is active: `ipconfig` in Command Prompt.
- Click **⟳ Refresh** in the GUI.

</details>

### 📝 Logs

```
%LOCALAPPDATA%\InterMux\intermux.log
```

---

## ⚠️ Known Limitations (Phase 1)

- **UDP traffic**: Not supported. Phase 2 will add SOCKS5 UDP relay.
- **Apps that ignore proxy env vars**: Some apps bypass system/env proxy settings. Phase 2 will add DLL-based socket binding.
- **Fuzzy App Finder**: The fuzzy app opener is planned for Phase 2. For now, provide the full absolute path or an exact command available in your system `PATH`.
- **Proxy Speed / Overhead**: The SOCKS5 proxy introduces a slight overhead, and the network speed may feel a little slow. We are actively finding out ways to improve the proxy throughput.

---

## 📄 License

MIT — see [license](../license)

---

<div align="center">
  <strong>Made with ❤️ for the Windows community</strong><br>
  <em>Linux version: see the <a href="../README.md">root README</a></em>
</div>
