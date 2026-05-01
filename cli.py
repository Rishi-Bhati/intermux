#!/usr/bin/env python3
"""
InterMux CLI — Network Interface Binding
Assigns applications to specific network interfaces using network namespaces.
Apps are launched as the original (non-root) user to preserve user profiles.
"""

import os
import sys
import subprocess
import argparse
import hashlib
import shutil

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), ".")))

from core.interface import get_active_interfaces
from core.router import clear_custom_routing_tables, check_existing_routing_tables
import core.platform_utils as plat


def run_cmd(cmd: str) -> str:
    """Run a shell command, print errors, return stdout."""
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        stderr = result.stderr.strip()
        if result.returncode != 0 and "Cannot find device" not in stderr \
                and "No such file or directory" not in stderr:
            print(f"[!] {cmd}\n    -> {stderr}")
        return result.stdout.strip()
    except Exception as e:
        print(f"[X] Exception while running '{cmd}': {e}")
        return ""


def list_interfaces():
    """Lists all active network interfaces."""
    print("--- Active Network Interfaces ---")
    interfaces = get_active_interfaces()
    if not interfaces:
        print("No active network interfaces found.")
        return

    for iface in interfaces:
        if iface["flag"] == "UP" and iface["ip_addresses"]:
            print(f"\nInterface: {iface['name']}")
            print(f"  Status:    {iface['flag']}")
            print(f"  Type:      {iface['type']}")
            print(f"  IPs:       {', '.join(iface['ip_addresses'])}")
            print(f"  Gateways:  {', '.join(iface['gateways']) if iface['gateways'] else 'N/A'}")


def assign_app(app: str, iface: str):
    """Assigns an application to a specific network interface."""
    app_name = os.path.basename(app).lower()

    if "chromium" in app_name:
        print("[X] Chromium is not supported due to its sandboxing architecture.")
        return

    if not os.path.isabs(app):
        resolved = shutil.which(app)
        if resolved:
            app = resolved
        elif not os.path.exists(app):
            print(f"[X] Could not find '{app}' — provide a full path or a command name in PATH.")
            return
    elif not os.path.exists(app):
        print(f"[X] Application path not found: {app}")
        return

    app_name = os.path.basename(app).lower()  # refresh after resolution
    user = plat.get_invoking_user()


    # --- Handle already-running instances ---
    pids = plat.find_running_instances(app)
    use_new_profile = False
    if pids:
        print(f"\n[!] '{os.path.basename(app)}' is already running (PID(s): {pids}).")
        print("    Options:")
        print("    1) Close current instance and reopen on the selected network (uses your default profile)")
        print("    2) Open a new instance with a separate profile")
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

    print(f"[+] Assigning '{app}' to interface '{iface}'...")

    # --- Namespace setup ---
    iface_hash = hashlib.md5(iface.encode()).hexdigest()[:8]
    ns = f"ns_{iface_hash}"
    veth0 = f"veth0_{iface_hash}"
    veth1 = f"veth1_{iface_hash}"
    subnet_byte = hash(iface) % 253 + 1

    run_cmd(f"ip netns del {ns}")
    run_cmd(f"ip link del {veth0}")
    run_cmd(f"ip link del {veth1}")

    run_cmd(f"ip netns add {ns}")
    run_cmd(f"ip link add {veth0} type veth peer name {veth1}")
    run_cmd(f"ip link set {veth1} netns {ns}")
    run_cmd(f"ip addr add 10.0.{subnet_byte}.1/24 dev {veth0}")
    run_cmd(f"ip link set {veth0} up")
    run_cmd(f"ip netns exec {ns} ip addr add 10.0.{subnet_byte}.2/24 dev {veth1}")
    run_cmd(f"ip netns exec {ns} ip link set {veth1} up")
    run_cmd(f"ip netns exec {ns} ip link set lo up")
    run_cmd(f"ip netns exec {ns} ip route add default via 10.0.{subnet_byte}.1")

    run_cmd(f"iptables -t nat -A POSTROUTING -s 10.0.{subnet_byte}.0/24 -o {iface} -j MASQUERADE")
    run_cmd("iptables -A FORWARD -j ACCEPT")
    run_cmd("sysctl -w net.ipv4.ip_forward=1")

    # Write proper resolv.conf
    ns_etc = f"/etc/netns/{ns}"
    plat.write_namespace_resolv_conf(ns_etc)

    # --- Build launch command (drops to original user) ---
    env = plat.get_display_env()
    env_pairs = " ".join(f'{k}="{v}"' for k, v in env.items())

    extra_flags = ""
    if "firefox" in app_name:
        if use_new_profile:
            profile_dir = plat.get_or_create_firefox_profile(iface, user["home"])
            extra_flags = f"--no-remote --profile {profile_dir}"
        else:
            extra_flags = "--no-remote"

    launch_cmd = (
        f"ip netns exec {ns} "
        f"sudo -u {user['username']} "
        f"env {env_pairs} HOME={user['home']} "
        f"{app} {extra_flags}"
    ).strip()

    print(f"[+] Starting as user '{user['username']}' in namespace '{ns}'...")
    subprocess.Popen(launch_cmd, shell=True)
    print(f"[✓] '{os.path.basename(app)}' launched via interface '{iface}'")


def clear_all_paths():
    """Clears all custom routing tables."""
    if not check_existing_routing_tables():
        print("[i] No custom routing tables found.")
        return
    print("[+] Clearing routing tables...")
    clear_custom_routing_tables()
    print("[✓] All paths cleared.")


def reset_system():
    """Removes all veth interfaces, namespaces, and routing tables."""
    print("[+] Resetting system...")
    clear_custom_routing_tables()
    run_cmd("ip link del veth0 2>/dev/null")

    for iface in get_active_interfaces():
        iface_hash = hashlib.md5(iface["name"].encode()).hexdigest()[:8]
        run_cmd(f"ip netns del ns_{iface_hash} 2>/dev/null")

    print("[✓] System reset complete.")


def check_deps():
    """Checks and reports missing system dependencies."""
    missing = plat.get_missing_dependencies()
    if not missing:
        print("[✓] All dependencies are satisfied.")
        return
    print("[!] Missing dependencies:")
    for d in missing:
        print(f"    ✗ {d['tool']} — install with: {d['install_hint']}")


def main():
    if os.geteuid() != 0:
        print("[X] InterMux requires root for namespace setup.")
        print(f"    Run: sudo python3 {sys.argv[0]} ...")
        sys.exit(1)

    parser = argparse.ArgumentParser(
        description="InterMux — bind applications to specific network interfaces."
    )
    sub = parser.add_subparsers(dest="command", help="Available commands")

    sub.add_parser("list", help="List active network interfaces.") \
        .set_defaults(func=lambda _: list_interfaces())

    p_assign = sub.add_parser("assign", help="Assign an application to an interface.")
    p_assign.add_argument("--app", required=True, help="Path to the application.")
    p_assign.add_argument("--iface", required=True, help="Network interface name.")
    p_assign.set_defaults(func=lambda a: assign_app(a.app, a.iface))

    sub.add_parser("clear", help="Clear all assigned routing tables.") \
        .set_defaults(func=lambda _: clear_all_paths())

    sub.add_parser("reset", help="Reset everything to system defaults.") \
        .set_defaults(func=lambda _: reset_system())

    sub.add_parser("check", help="Check system dependencies.") \
        .set_defaults(func=lambda _: check_deps())

    args = parser.parse_args()
    if hasattr(args, "func"):
        args.func(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
