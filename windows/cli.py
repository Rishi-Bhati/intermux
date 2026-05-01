#!/usr/bin/env python3
"""
InterMux Windows — CLI
Assigns applications to specific network interfaces on Windows using SOCKS5 proxy binding.

Usage:
  python cli.py list
  python cli.py assign --app firefox --iface "Wi-Fi"
  python cli.py check
  python cli.py clear
  python cli.py reset
"""

import os
import sys
import argparse
import logging
import shutil

# Allow running from the repo root or the windows/ dir
_WIN_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT    = os.path.dirname(_WIN_DIR)
sys.path.insert(0, _ROOT)

from windows.core.interface import get_active_interfaces
import windows.core.platform_utils as plat
from windows.core.proxy_engine import proxy_registry
from windows.core.app_launcher import launch_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-7s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("intermux-win")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_list(_args):
    """List all active network interfaces."""
    print("\n--- Active Network Interfaces ---")
    ifaces = get_active_interfaces()
    active = [i for i in ifaces if i["flag"] == "UP" and i["ip_addresses"]]
    if not active:
        print("No active interfaces found.")
        return
    for iface in active:
        print(f"\n  Interface : {iface['name']}")
        print(f"  Type      : {iface['type']}")
        print(f"  IPs       : {', '.join(iface['ip_addresses'])}")
        print(f"  Gateways  : {', '.join(iface['gateways']) or 'N/A'}")
    print()


def cmd_assign(args):
    """Assign an app to a specific network interface."""
    app   = args.app
    iface_name = args.iface

    # Resolve app
    resolved = app if os.path.isabs(app) else shutil.which(app)
    if resolved is None or not os.path.exists(resolved or ""):
        if os.path.exists(app):
            resolved = os.path.abspath(app)
        else:
            print(f"[X] Could not find '{app}'. Provide a full path or a command name in PATH.")
            return

    app = resolved
    app_basename = os.path.basename(app).lower()

    # Find the interface
    ifaces = get_active_interfaces()
    iface  = next(
        (i for i in ifaces if i["name"].lower() == iface_name.lower() and i["flag"] == "UP"),
        None
    )
    if iface is None:
        print(f"[X] Interface '{iface_name}' not found or not UP.")
        print("    Run: python cli.py list")
        return

    ipv4s = [ip for ip in iface["ip_addresses"] if ":" not in ip]
    if not ipv4s:
        print(f"[X] No IPv4 address on '{iface_name}'. Cannot bind proxy.")
        return
    bind_ip = ipv4s[0].split("/")[0]

    # Check for already-running instances
    pids = plat.find_running_instances(app)
    use_new_profile = False

    if pids:
        print(f"\n[!] '{os.path.basename(app)}' is already running (PID(s): {pids}).")
        print("    Options:")
        print("    1) Close current instance and reopen on the selected network")
        print("    2) Open a new instance (separate profile for Firefox)")
        print("    3) Cancel")
        while True:
            choice = input("    Enter choice [1/2/3]: ").strip()
            if choice == "1":
                print("[+] Closing existing instances...")
                plat.kill_running_instances(app)
                use_new_profile = False
                break
            elif choice == "2":
                use_new_profile = True
                break
            elif choice == "3":
                print("[i] Cancelled.")
                return
            else:
                print("    Please enter 1, 2, or 3.")

    print(f"\n[+] Starting SOCKS5 proxy for interface '{iface_name}' (IP: {bind_ip})...")
    try:
        proc = launch_app(app, iface_name, bind_ip, use_new_profile=use_new_profile)
    except Exception as e:
        print(f"[X] Failed to launch '{app}': {e}")
        return

    proxy_port = proxy_registry.running_proxies().get(bind_ip, "?")
    print(f"[✓] '{os.path.basename(app)}' launched (PID {proc.pid})")
    print(f"    Proxy  : socks5://127.0.0.1:{proxy_port}")
    print(f"    Adapter: {iface_name} ({bind_ip})")
    print()
    print("    Note: ~1-2ms latency added per TCP connection (proxy overhead).")
    print("    This is imperceptible for browsing and downloads.")


def cmd_check(_args):
    """Check for required dependencies."""
    missing = plat.get_missing_dependencies()
    if not missing:
        print("[✓] All dependencies are satisfied.")
        return
    print("[!] Missing dependencies:")
    for d in missing:
        print(f"    ✗ {d['tool']} — install with: {d['install_hint']}")


def cmd_clear(_args):
    """Stop all running InterMux proxies."""
    proxies = proxy_registry.running_proxies()
    if not proxies:
        print("[i] No active InterMux proxies running.")
        return
    print(f"[+] Stopping {len(proxies)} proxy/proxies...")
    proxy_registry.stop_all()
    print("[✓] All proxies stopped.")


def cmd_reset(_args):
    """Stop all proxies and restore system state."""
    print("[+] Resetting InterMux...")
    proxy_registry.stop_all()
    print("[✓] Reset complete. All proxies stopped.")
    print("    Note: Any apps launched by InterMux continue to run normally —")
    print("    their traffic will use the system default interface until restarted.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    # Windows only
    if sys.platform != "win32":
        print("[X] This CLI is for Windows only.")
        print("    On Linux, use: python3 cli.py")
        sys.exit(1)

    # UAC check — binding sockets to specific adapters may need admin
    if not plat.is_admin():
        print("[!] InterMux may need Administrator privileges for full functionality.")
        print("    Re-run from an elevated command prompt, or approve the UAC prompt.")

    parser = argparse.ArgumentParser(
        description="InterMux for Windows — bind applications to specific network interfaces.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python cli.py list
  python cli.py assign --app firefox --iface "Wi-Fi"
  python cli.py assign --app "C:\\Path\\To\\app.exe" --iface "Ethernet"
  python cli.py check
  python cli.py clear
        """,
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list",  help="List active network interfaces.")         .set_defaults(func=cmd_list)
    sub.add_parser("check", help="Check required dependencies.")            .set_defaults(func=cmd_check)
    sub.add_parser("clear", help="Stop all running InterMux proxies.")      .set_defaults(func=cmd_clear)
    sub.add_parser("reset", help="Reset everything to defaults.")           .set_defaults(func=cmd_reset)

    p_assign = sub.add_parser("assign", help="Assign an app to an interface.")
    p_assign.add_argument("--app",   required=True, help="Path or name of the application.")
    p_assign.add_argument("--iface", required=True, help='Network interface name (e.g. "Wi-Fi").')
    p_assign.set_defaults(func=cmd_assign)

    args = parser.parse_args()
    if hasattr(args, "func"):
        args.func(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
