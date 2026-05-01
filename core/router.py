#!/usr/bin/env python3
import sys
import os
import subprocess
import re
import ipaddress
import shutil
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from core.interface import get_active_interfaces

_IP_CMD = shutil.which("ip") or "/usr/sbin/ip"

_RT_TABLES_CANDIDATES = [
    "/etc/iproute2/rt_tables",
    "/usr/share/iproute2/rt_tables",
]
RT_TABLES_PATH = next((p for p in _RT_TABLES_CANDIDATES if os.path.exists(p)), "/etc/iproute2/rt_tables")
BASE_TABLE_ID = 100
BASE_PRIORITY = 1000



def get_network(ip_with_cidr):
    net = ipaddress.ip_interface(ip_with_cidr).network
    return str(net)

def run_cmd(cmd):
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.stderr and not result.returncode == 0:
        print(f"[!] {cmd} -> {result.stderr.strip()}")
    return result.stdout.strip()



def extract_ip_and_prefix(ip_with_cidr):
    if '/' in ip_with_cidr:
        return ip_with_cidr.split('/')
    return ip_with_cidr, '24'

def ensure_routing_table(table_id, name):
    with open(RT_TABLES_PATH, 'r+') as f:
        lines = f.read().splitlines()
        entry = f"{table_id} {name}"
        if entry not in lines:
            f.write(f"\n{entry}\n")
            print(f"[+] Added routing table entry: {entry}")
    return True

# def setup_interface_routing(name, ip_with_cidr, gateway, table_id):
#     ip, prefix = extract_ip_and_prefix(ip_with_cidr)
#     table_name = f"{name}_rt"

#     ensure_routing_table(table_id, table_name)

#     run_cmd(f"ip route flush table {table_id}")
#     run_cmd(f"ip rule del from {ip} table {table_id} priority {BASE_PRIORITY + table_id} 2>/dev/null")

#     run_cmd(f"ip route add {ip}/{prefix} dev {name} scope link table {table_id}")
#     run_cmd(f"ip route add default via {gateway} dev {name} table {table_id}")
#     run_cmd(f"ip rule add from {ip} table {table_id} priority {BASE_PRIORITY + table_id}")

#     print(f"[✓] Routing set for {name} ({ip}/{prefix}) via {gateway} [table {table_id}]")

def setup_interface_routing(name, ip_with_cidr, gateway, table_id):
    ip, prefix = extract_ip_and_prefix(ip_with_cidr)
    table_name = f"{name}_rt"

    ensure_routing_table(table_id, table_name)

    run_cmd(f"{_IP_CMD} route flush table {table_id}")
    run_cmd(f"{_IP_CMD} rule del from {ip} table {table_id} priority {BASE_PRIORITY + table_id} 2>/dev/null")

    network = get_network(ip_with_cidr)
    run_cmd(f"{_IP_CMD} route add {network} dev {name} scope link table {table_id}")
    run_cmd(f"{_IP_CMD} route add default via {gateway} dev {name} table {table_id}")
    run_cmd(f"{_IP_CMD} rule add from {ip} table {table_id} priority {BASE_PRIORITY + table_id}")

    print(f"[✓] Routing set for {name} ({ip}/{prefix}) via {gateway} [table {table_id}]")


def check_existing_routing_tables():
    with open(RT_TABLES_PATH, 'r') as f:
        lines = f.read().splitlines()

    for line in lines:
        if line.strip() and not line.strip().startswith("#"):
            parts = line.strip().split()
            if len(parts) == 2:
                table_id, name = parts
                if name.endswith('_rt'):
                    return True
    return False

def clear_custom_routing_tables():
    if os.geteuid() != 0:
        print("[X] Run this script as root.")
        return

    # Read existing tables
    with open(RT_TABLES_PATH, 'r') as f:
        lines = f.read().splitlines()

    # Filter out custom tables (ending in _rt)
    remaining_lines = []
    custom_tables = []

    for line in lines:
        if line.strip() and not line.strip().startswith("#"):
            parts = line.strip().split()
            if len(parts) == 2:
                table_id, name = parts
                if name.endswith('_rt'):
                    custom_tables.append((table_id, name))
                    continue
        remaining_lines.append(line)

    # Write back only non-custom entries
    with open(RT_TABLES_PATH, 'w') as f:
        f.write("\n".join(remaining_lines) + "\n")

    # Clear associated routes and rules
    for table_id, name in custom_tables:
        try:
            run_cmd(f"{_IP_CMD} route flush table {table_id}")
            run_cmd(f"{_IP_CMD} rule del table {table_id}")
            print(f"[✓] Cleared routing table {table_id} ({name})")
        except Exception as e:
            print(f"[!] Failed to clear table {table_id}: {e}")

def main():
    if os.geteuid() != 0:
        print("[X] Run this script as root.")
        return

    interfaces = get_active_interfaces()
    table_id = BASE_TABLE_ID

    for iface in interfaces:
        if iface['flag'] != 'UP':
            continue

        ipv4s = [ip for ip in iface['ip_addresses'] if ':' not in ip]
        if not ipv4s or not iface['gateways']:
            continue

        ip_with_cidr = ipv4s[0]
        gateway = iface['gateways'][0]

        setup_interface_routing(iface['name'], ip_with_cidr, gateway, table_id)
        table_id += 1

if __name__ == "__main__":
    main()
