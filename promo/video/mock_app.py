"""Run the real InterMux GUI with fake interfaces, bindings and traffic, for promo screenshots.

Nothing here touches the system: every read the GUI makes (interfaces, state, processes, counters,
desktop entries, history) is replaced with made-up data. Usage, from the repository root:

    python3 -m promo.video.mock_app [--scene empty|queue|bindings]
"""

from __future__ import annotations

import argparse
import math
import os
import random
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from intermux import state as state_mod  # noqa: E402
from intermux.apps import desktop_entries  # noqa: E402
from intermux.apps.desktop_entries import AppEntry  # noqa: E402
from intermux.system import adopt, deps, netinfo, netns, procs, session, traffic  # noqa: E402
from intermux.system.netinfo import Address, Interface  # noqa: E402

IFACES = [
    Interface("enp3s0", "ethernet", True, "up", ["UP"], "aa:00:00:00:00:01", 1500,
              [Address("inet", "192.168.0.24", 24, None, "global")], True, ["192.168.0.1"]),
    Interface("wlan0", "wifi", True, "up", ["UP"], "aa:00:00:00:00:02", 1500,
              [Address("inet", "192.168.1.42", 24, None, "global")], True, ["192.168.1.1"]),
    Interface("usb0", "usb", True, "up", ["UP"], "aa:00:00:00:00:03", 1500,
              [Address("inet", "172.20.10.4", 28, None, "global")], True, ["172.20.10.1"]),
    Interface("wg0", "vpn", True, "unknown", ["UP"], None, 1420,
              [Address("inet", "10.8.0.2", 32, None, "global")], False, []),
]

APPS = {
    "firefox": ("Firefox", "/usr/lib/firefox/firefox", "Browse the web"),
    "qbittorrent": ("qBittorrent", "/usr/bin/qbittorrent", "BitTorrent client"),
    "spotify": ("Spotify", "/opt/spotify/spotify", "Music for every moment"),
    "zoom": ("Zoom", "/opt/zoom/zoom", "Video meetings"),
    "code": ("Visual Studio Code", "/usr/bin/code", "Code editing"),
    "steam": ("Steam", "/usr/bin/steam", "Games"),
    "discord": ("Discord", "/usr/bin/discord", "Chat"),
    "chromium": ("Chromium", "/usr/bin/chromium", "Web browser"),
    "thunderbird": ("Thunderbird", "/usr/bin/thunderbird", "Mail"),
    "vlc": ("VLC", "/usr/bin/vlc", "Media player"),
    "obs": ("OBS Studio", "/usr/bin/obs", "Streaming"),
    "telegram": ("Telegram", "/usr/bin/telegram-desktop", "Messaging"),
}

# scene -> bindings (iface, app key, pid), adoptions (iface, app key, pids), dns, limits
SCENES = {
    "empty": ([], [], {}, {}),
    "queue": ([], [], {}, {}),
    "bindings": (
        [("enp3s0", "qbittorrent", 48211), ("wlan0", "firefox", 48377), ("usb0", "zoom", 48502)],
        [("wlan0", "spotify", [31877, 31902])],
        {"wlan0": ["1.1.1.1", "1.0.0.1"], "usb0": ["9.9.9.9", "149.112.112.112"]},
        {"enp3s0": {"down": 80.0, "up": 10.0}},
    ),
}

# Rough shape of each lane's traffic, in bytes per second (down, up).
RATES = {
    "enp3s0": (11.4e6, 1.3e6), "wlan0": (2.6e6, 0.31e6), "usb0": (0.92e6, 0.48e6), "wg0": (0.0, 0.0),
}


def ns_inode(name: str) -> int:
    return 4026530000 + sum(ord(c) for c in name)


def install(scene: str) -> None:
    binds, adopts, dns, limits = SCENES[scene]
    st = state_mod.State()
    for i, (iface, key, pid) in enumerate(binds):
        name, path, _ = APPS[key]
        st.subnets[iface] = f"10.200.{i + 3}.0/24"
        st.bindings.append(state_mod.Binding(iface=iface, app=path, app_name=os.path.basename(path),
                                             namespace=netns.namespace_name(iface), subnet=st.subnets[iface],
                                             table=1000 + i + 3, priority=1000 + i + 3, pid=pid))
    for i, (iface, key, pids) in enumerate(adopts):
        _, path, _ = APPS[key]
        st.adoptions.append(state_mod.Adoption(id=f"{i:02x}c3e9a1", iface=iface, app=path,
                                               app_name=os.path.basename(path), slot=i, group=f"intermux/{i}",
                                               mark=0x1000 + i, table=2000 + i, priority=2000 + i,
                                               uid=os.getuid(), pids=pids))
    st.dns.update(dns)
    st.limits.update(limits)

    netinfo.list_interfaces = lambda *a, **k: list(IFACES)
    state_mod.load_state = lambda *a, **k: st
    table = [procs.Proc(pid, 1, os.path.basename(APPS[key][1])[:15], os.getuid(), APPS[key][1],
                        ns_inode(netns.namespace_name(iface))) for iface, key, pid in binds]
    procs.list_procs = lambda: list(table)
    procs.netns_inode = ns_inode
    adopt.read_group_pids = lambda group: [p for _, _, ps in adopts for p in ps][:2]
    deps.missing_required = lambda *a, **k: []

    real_detect = session.detect_session

    def fake_session(*a, **k):
        s = real_detect(*a, **k)
        s.session_type, s.display = "wayland", ":1"
        return s
    session.detect_session = fake_session

    entries = [AppEntry(name, comment, key, [path], f"/usr/share/applications/{key}.desktop", path, True, "")
               for key, (name, path, comment) in APPS.items()]
    desktop_entries.scan_desktop_entries = lambda *a, **k: list(entries)

    # Byte counters that grow at a wobbly rate, so the speed columns and graphs move.
    start = time.monotonic()
    totals = {}
    veths = {netns.veth_names(iface)[0]: iface for iface, _, _ in binds}
    rng = random.Random(7)

    def counters(names, base=None):
        now = time.monotonic() - start
        out = {}
        for name in names:
            lane = veths.get(name, name)
            down, up = RATES.get(lane, (0.0, 0.0))
            if name in veths:
                down, up = down * 0.93, up * 0.9
            wob = 0.72 + 0.28 * math.sin(now * 0.9 + len(name)) + rng.uniform(-0.12, 0.12)
            rx, tx = totals.get(name, (10_000_000_000, 2_000_000_000))
            rx, tx = rx + int(down * wob), tx + int(up * (wob + rng.uniform(-0.1, 0.1)))
            totals[name] = (rx, tx)
            # read_counters reports rx/tx; a veth's host end is the other way round.
            out[name] = traffic.Counters(tx, rx) if name in veths else traffic.Counters(rx, tx)
        return out
    traffic.read_counters = counters
    traffic.link_speed_bytes = lambda name, base=None: None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scene", default="bindings", choices=sorted(SCENES))
    args = parser.parse_args()
    home = Path(tempfile.mkdtemp(prefix="intermux-promo-"))
    os.environ["HOME"] = str(home)
    os.environ["XDG_STATE_HOME"] = str(home / ".local/state")
    hist = home / ".local/state/intermux"
    hist.mkdir(parents=True)
    (hist / "history.json").write_text(
        '[{"app": "/usr/lib/firefox/firefox", "name": "Firefox", "iface": "wlan0", "dns": "Cloudflare"}]')
    install(args.scene)

    import tkinter as tk
    from intermux.ui import app as ui_app
    root = tk.Tk()
    gui = ui_app.InterMuxApp(root)
    if args.scene == "queue":
        def fill():
            gui.iface_var.set(gui.iface_combo.cget("values")[1] if gui.iface_combo.cget("values") else "")
            for iface, key in (("enp3s0", "qbittorrent"), ("wlan0", "firefox"), ("usb0", "zoom")):
                name, path, _ = APPS[key]
                gui._pending.append({"iface": iface, "app": path, "name": name, "policy": "ask",
                                     "dns": "keep", "limit": {}})
            gui._render_pending()
        root.after(1200, fill)
    if os.environ.get("PROMO_OPEN") == "picker":
        root.after(1500, gui._on_pick)
    if os.environ.get("PROMO_OPEN") == "running":
        root.after(1500, lambda: ui_app._ask_choice(
            root, "Firefox is already running",
            "Firefox is already running. Close it and open it again on wlan0, open a separate profile, "
            "or shift the running copy onto wlan0 without restarting it?",
            ui_app.already_running_choices()))
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
