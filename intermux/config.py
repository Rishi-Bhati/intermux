from __future__ import annotations

from pathlib import Path

VERSION = "2.0.1"

STATE_DIR = Path("/var/lib/intermux")
STATE_FILE = STATE_DIR / "state.json"
RUN_DIR = Path("/run/intermux")
LOCK_FILE = RUN_DIR / "helper.lock"
NETNS_DIR = Path("/var/run/netns")          # where `ip netns add` keeps namespace handles
ETC_NETNS_DIR = Path("/etc/netns")          # per-namespace resolv.conf

INSTALL_BIN = Path("/usr/lib/intermux/intermux")   # installed (onedir) binary; polkit annotates this path

SUBNET_BASE_OCTET = 200                     # subnets are 10.200.N.0/24, N in 1..254
TABLE_BASE = 1000                           # routing table id = TABLE_BASE + N
PRIORITY_BASE = 1000                        # ip rule priority = PRIORITY_BASE + N

NS_NAME_RE = r"^ns_[0-9a-f]{8}$"
VETH_HOST_RE = r"^veth0_[0-9a-f]{8}$"

ENV_ALLOWLIST = frozenset({
    "DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "XDG_SESSION_TYPE",
    "XDG_CURRENT_DESKTOP", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS",
    "PULSE_SERVER", "PIPEWIRE_REMOTE", "LANG", "LC_ALL", "GTK_THEME", "QT_QPA_PLATFORM",
})

APP_PATH_ENV = "/usr/local/bin:/usr/bin:/bin"
PUBLIC_IP_URL = "https://api.ipify.org"
VIRTUAL_PREFIXES = ("lo", "veth", "br-", "docker", "virbr", "vmnet", "vboxnet", "tun", "tap")
APP_LOG_DIR_REL = Path(".local/state/intermux/logs")   # relative to the user's home
