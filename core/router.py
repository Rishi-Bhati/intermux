#!/usr/bin/env python3
import sys
import os
import subprocess
import re
import ipaddress
import shutil
import platform
import logging

# Ensure we can import from core even if run directly
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from core.interface import get_active_interfaces

# Global OS Check
IS_MAC = platform.system() == "Darwin"
_IP_CMD = shutil.which("ip") or "/usr/sbin/ip"

# Linux-only routing table paths
_RT_TABLES_CANDIDATES = [
    "/etc/iproute2/rt_tables",
    "/usr/share/iproute2/rt_tables",
]
RT_TABLES_PATH = next((p for p in _RT_TABLES_CANDIDATES if os.path.exists(p)), "/etc/iproute2/rt_tables")
BASE_TABLE_ID = 100
BASE_PRIORITY = 1000

def get_network(ip_with_cidr):
    """Calculates the network address from a CIDR string."""
    try:
        net = ipaddress.ip_interface(ip_with_cidr).network
        return str(net)
    except ValueError:
        return ip_with_cidr 

def run_cmd(cmd):
    """Executes a shell command and logs errors."""
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.stderr and not result.returncode == 0:
        # Ignore "File exists" errors on Mac/Linux if route is already present
        if not any(msg in result.stderr for msg in ["File exists", "already in table", "entry exists"]):
            print(f"[!] {cmd} -> {result.stderr.strip()}")
    return result.stdout.strip()

def extract_ip_and_prefix(ip_with_cidr):
    """Splits IP from CIDR prefix."""
    if '/' in ip_with_cidr:
        return ip_with_cidr.split('/')
    return ip_with_cidr, '24'

def ensure_routing_table(table_id, name):
    """Ensures the Linux iproute2 table exists. No-op on macOS."""
    if IS_MAC:
        return True
    
    try:
        with open(RT_TABLES_PATH, 'r+') as f:
            lines = f.read().splitlines()
            entry = f"{table_id} {name}"
            if entry not in lines:
                f.write(f"\n{entry}\n")
                print(f"[+] Added routing table entry: {entry}")
    except:
        pass
    return True

def setup_interface_routing(name, ip_with_cidr, gateway, table_id):
    """Performs the actual routing setup for the specific OS."""
    ip, prefix = extract_ip_and_prefix(ip_with_cidr)
    network = get_network(ip_with_cidr)

    if IS_MAC:
        # macOS Strategy: Use the 'route' command to bind the network to the interface.
        # This is the Darwin alternative to Linux Policy Based Routing.
        run_cmd(f"sudo route delete {network} > /dev/null 2>&1")
        run_cmd(f"sudo route add {network} -interface {name}")
        print(f"[✓] macOS: Network {network} now routed via {name}")
        return

    # Linux Strategy (Original)
    table_name = f"{name}_rt"
    ensure_routing_table(table_id, table_name)
    run_cmd(f"{_IP_CMD} route flush table {table_id}")
    run_cmd(f"{_IP_CMD} rule del from {ip} table {table_id} priority {BASE_PRIORITY + table_id} 2>/dev/null")
    run_cmd(f"{_IP_CMD} route add {network} dev {name} scope link table {table_id}")
    run_cmd(f"{_IP_CMD} route add default via {gateway} dev {name} table {table_id}")
    run_cmd(f"{_IP_CMD} rule add from {ip} table {table_id} priority {BASE_PRIORITY + table_id}")
    print(f"[✓] Linux: Routing set for {name} ({ip}/{prefix}) via {gateway}")

def main():
    """Main execution loop with explicit macOS support."""
    if os.geteuid() != 0:
        print("[X] Error: You must run this script with sudo.")
        return

    print("[*] Detecting active interfaces...")
    interfaces = get_active_interfaces()
    
    if not interfaces:
        print("[!] No active interfaces found.")
        return

    table_id = BASE_TABLE_ID

    for iface in interfaces:
        if iface['flag'] != 'UP':
            continue

        # Extract IPv4 address
        ipv4s = [ip for ip in iface['ip_addresses'] if ':' not in ip]
        if not ipv4s or not iface['gateways']:
            continue

        ip_with_cidr = ipv4s[0]
        # Skip IPv6 gateways for standard routing
        gateway = next((g for g in iface['gateways'] if ':' not in g), iface['gateways'][0])

        print(f"[*] Configuring {iface['name']} (IP: {ip_with_cidr}, GW: {gateway})")
        setup_interface_routing(iface['name'], ip_with_cidr, gateway, table_id)
        table_id += 1

if __name__ == "__main__":
    main()