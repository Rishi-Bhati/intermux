<div align="center">

# 🌐 InterMux

### Advanced Network Interface Management for Linux

[![Python Version](https://img.shields.io/badge/python-3.6%2B-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](https://opensource.org/license/mit)
[![Platform Linux](https://img.shields.io/badge/platform-Linux-orange.svg)](https://www.linux.org/)
[![Platform Windows](https://img.shields.io/badge/platform-Windows%2010%2F11-blue.svg)](windows/README.md)
[![Privilege Model](https://img.shields.io/badge/privileges-root%20for%20network%20only-yellow.svg)](https://en.wikipedia.org/wiki/Superuser)

<p align="center">
  <strong>Bind applications to specific network interfaces with ease</strong>
</p>

[Features](#-features) • [Installation](#-installation) • [Usage](#-usage) • [Architecture](#-architecture) • [Contributing](#-contributing)

</div>

---

## 📋 Overview

**InterMux** is a powerful Linux utility that enables you to bind applications to specific network interfaces. Whether you need to route your browser through Wi-Fi while keeping your development server on Ethernet, or isolate applications for security testing, InterMux makes it simple.

> **Privacy Note:** InterMux sets up network namespaces as root, but **launches all applications as your regular user** — your bookmarks, cookies, and app settings are always preserved.

### 🎯 Use Cases

- **Multi-WAN Management**: Route different applications through different internet connections
- **Network Testing**: Test applications on specific network interfaces
- **Security Isolation**: Isolate applications in separate network namespaces
- **Bandwidth Management**: Control which apps use which network connections
- **Development**: Test network-dependent applications across different interfaces


## 💡 Why I Made It

I was downloading a large file via torrent, and while waiting, I opened YouTube to pass the time.
But the videos were stuck on low quality, buffering like crazy — even though my connection showed 300 Mbps.

Turns out, the torrent was consuming all the bandwidth, and YouTube was left starving.
That’s when I had the idea: “What if I could assign different apps to different networks?”

After some research, I found that while Linux supports advanced networking, there wasn’t a simple tool to do what I needed — especially one that was GUI-friendly and straightforward.

So I built InterMux:
A utility that lets me bind any app to a specific interface with ease — no messy configs, no guesswork, just full control.

## ✨ Features

<table>
<tr>
<td>

### 🖥️ Core Features
- 🔍 **Auto-detection** of all network interfaces
- 🛣️ **Custom routing tables** per interface
- 🔒 **Network namespace isolation**
- 📊 **Real-time interface monitoring**
- 🎛️ **Both CLI and GUI interfaces**
- 👤 **User-preserving** — apps keep your profile & data

</td>
<td>

### 🌟 Interface Support
- 📶 **Wi-Fi** (wlan*, wl*)
- 🔌 **Ethernet** (eth*, en*)
- 📱 **USB Tethering**
- 🔵 **Bluetooth Tethering**
- 🌐 **Virtual Interfaces**

</td>
</tr>
</table>

## 🚀 Installation

### Prerequisites

<details>
<summary><strong>Ubuntu / Debian / Mint</strong></summary>

```bash
sudo apt update
sudo apt install -y python3 python3-pip iproute2 iptables policykit-1 x11-xserver-utils
```
</details>

<details>
<summary><strong>Arch / EndeavourOS / Manjaro</strong></summary>

```bash
sudo pacman -S python python-pip iproute2 iptables polkit xorg-xhost
```
</details>

<details>
<summary><strong>Fedora / RHEL / Rocky</strong></summary>

```bash
sudo dnf install -y python3 python3-pip iproute iptables polkit xorg-x11-server-utils
```
</details>

<details>
<summary><strong>openSUSE</strong></summary>

```bash
sudo zypper install python3 python3-pip iproute2 iptables polkit xorg-x11-xhost
```
</details>

- Note for Ubuntu 24.04+ users: Some Python modules (like brotli) may require system-level installation.

```bash
# If pip fails
sudo apt install python3-brotli
```

### Clone and Setup

```bash
# Clone the repository
git clone https://github.com/Rishi-Bhati/intermux.git
cd intermux

# Python Virtual environment
python3 -m venv venv
source venv/bin/activate

# Install Python dependencies
pip3 install -r requirements.txt
```

### Verify Dependencies

```bash
python3 cli.py check
```

## 🧩 Packeges Required (If everything above fails)

- You can explicitly install these packeges, if all the above given installation steps fails. 
```
Brotli==1.1.0
click==8.2.1
markdown2==2.5.3
MarkupSafe==3.0.2
minijinja==2.11.0
page==0.3
PyYAML==6.0.2
tk==0.1.0
```


## 📖 Usage

### 🎨 GUI Mode (Recommended)

```bash
python3 gui/app.py
```

The GUI requests root via `pkexec` (falls back to `sudo`). Apps always launch as **your regular user** — your profile data is preserved.

<details>
<summary><strong>GUI Features</strong></summary>

- **Interface Selection**: Dropdown with all active physical interfaces (virtual adapters like veth/docker are auto-hidden)
- **Application Binding**: Enter app path and assign to an interface
- **Running App Detection**: Prompts to close & reopen (default profile) or open new instance
- **Session Awareness**: Auto-detects X11 or Wayland
- **Dependency Banner**: Warns about missing system tools

</details>

### 💻 CLI Mode

#### 1. List Active Interfaces

```bash
sudo python3 cli.py list
```

#### 2. Assign Application to Interface

```bash
sudo python3 cli.py assign --app /usr/lib/firefox/firefox --iface wlan0
```

If Firefox is already open, you'll be prompted:

```
[!] 'firefox' is already running (PID(s): [12345]).
    Options:
    1) Close current instance and reopen on the selected network (uses your default profile)
    2) Open a new instance with a separate profile
    3) Cancel
    Enter choice [1/2/3]:
```

#### 3. Check Dependencies

```bash
python3 cli.py check
```

#### 4. Clear All Paths

```bash
sudo python3 cli.py clear
```

#### 5. Reset Everything

```bash
sudo python3 cli.py reset
```

## 🎥 Tutorial Video

<video controls width="100%">
  <source src="https://res.cloudinary.com/dzsghc33d/video/upload/v1752167379/Screencast_20250710_223520_gfnqkq.webm" type="video/webm">
  Your browser does not support the video tag.
</video>


## 🎥 Tutorial Video

![Screencast_20250710_223520_gfnqkq](https://github.com/user-attachments/assets/c018bddd-7f32-4471-a3f1-da75d4463c3c)


## 🏗️ Architecture

```
intermux/
├── core/                   # Core functionality
│   ├── interface.py        # Network interface detection (cross-distro)
│   ├── router.py           # Routing table management
│   └── platform_utils.py   # ★ Distro detection, display/DNS/user helpers
├── gui/                    # GUI components
│   ├── app.py              # Main GUI application
│   └── gui.py              # Legacy GUI interface
├── cli.py                  # CLI entrypoint
├── requirements.txt        # Python dependencies
└── README.md               # This file
```

### 🔧 How It Works

1. **Interface Detection**: Scans system for all network interfaces using `ip` commands
2. **Routing Tables**: Creates custom routing tables in `/etc/iproute2/rt_tables`
3. **Network Namespaces**: Isolates network traffic using Linux network namespaces
4. **Virtual Interfaces**: Uses veth pairs to connect namespaces to physical interfaces
5. **IP Forwarding**: Configures NAT/masquerading for namespace connectivity
6. **Privilege Separation**: Namespace setup runs as root; apps launch as **your user** via `sudo -u`
7. **DNS Resolution**: Writes real upstream DNS servers to namespace (handles `systemd-resolved` stubs)

### 🔐 Privilege Model

| Operation | Runs As |
|-----------|---------|
| Create network namespace | root |
| Configure veth / routing / NAT | root |
| Write namespace resolv.conf | root |
| **Launch your application** | **your regular user** |

## 🛠️ Advanced Configuration

### Custom Routing Table IDs

Edit `core/router.py`:
```python
BASE_TABLE_ID = 100  # Starting table ID
BASE_PRIORITY = 1000 # Starting priority
```

## 🐛 Troubleshooting

<details>
<summary><strong>GUI doesn't launch on Wayland</strong></summary>

InterMux auto-detects Wayland. If issues occur, ensure `xwayland` is installed:

```bash
# Arch
sudo pacman -S xorg-xwayland
# Ubuntu/Debian
sudo apt install xwayland
```
</details>

<details>
<summary><strong>Permission denied errors</strong></summary>

```bash
sudo python3 cli.py assign --app /usr/lib/firefox/firefox --iface wlan0
```
</details>

<details>
<summary><strong>Interface not detected</strong></summary>

```bash
ip link show
sudo ip link set <interface> up
```
</details>

<details>
<summary><strong>No internet in namespace / DNS failures</strong></summary>

```bash
python3 cli.py check
sudo sysctl -w net.ipv4.ip_forward=1
```
</details>

### 📝 Logs and Debugging

InterMux automatically logs all activity, commands, and errors to a persistent log file:
```bash
~/.local/share/intermux/intermux.log
```
This log includes detailed debug information and full stack traces, which is highly recommended to include when reporting issues.

## ⚠️ Known Limitations

### Browser Compatibility

- ✅ **Firefox**: Fully supported — uses your existing profile, prompts if already running
- ❌ **Chromium**: Not supported due to sandboxing conflicts with network namespaces
- ✅ **Most other apps**: GUI and CLI applications work correctly

### Platform Support

- ✅ **Linux (all major distros)**: Arch, Ubuntu, Debian, Fedora, openSUSE, Manjaro, EndeavourOS, etc.
- ✅ **X11 and Wayland**: Both session types supported
- ✅ **Windows 10 / 11**: Phase 1 supported via SOCKS5 proxy binding — see [windows/README.md](windows/README.md)

### System Requirements

- Root privileges for network namespace operations
- Linux kernel with `CONFIG_NET_NS`
- `iproute2` for network management

## 🤝 Contributing

Contributions are welcome! Please submit a Pull Request.

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/AmazingFeature`)
3. Commit your changes
4. Push and open a Pull Request

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](license) file for details.

## 🙏 Acknowledgments

- Built with Python and Tkinter
- Uses Linux networking stack (iproute2)
- Inspired by the need for better network interface management

## 📞 Support

- 🐛 [Report bugs](https://github.com/Rishi-Bhati/intermux/issues)
- 💡 [Request features](https://github.com/Rishi-Bhati/intermux/issues)

---

## 🪟 Windows Support

InterMux now supports Windows 10 and 11 via a SOCKS5 proxy engine.
See **[windows/README.md](windows/README.md)** for full installation and usage instructions.

```
windows/
├── gui/app.py          ← GUI (same dark theme)
├── cli.py              ← CLI (same commands)
├── core/               ← Windows-specific engine
└── intermux_windows.spec  ← PyInstaller → .exe
```

---

<div align="center">
  <strong>Made with ❤️ for the Linux and Windows communities</strong>
</div>
