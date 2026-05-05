# intermux/core/interface.py

import subprocess
import re
import logging
import shutil
import platform
import os

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

# --- 1. GLOBAL DEFINITIONS (Fixes NameError and Path Issues) ---
IS_MAC = platform.system() == "Darwin"
_IP_CMD = shutil.which("ip") or "/usr/sbin/ip"

def _run_command(command_parts, check_return=True, suppress_errors=False):
    """
    Helper function to run a shell command and capture its output.
    """
    try:
        result = subprocess.run(
            command_parts,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=check_return,
            encoding='utf-8'
        )
        return result.stdout.strip()
    except FileNotFoundError:
        if not IS_MAC: # Only log error if we are on a system that expects these tools
            logging.error(f"Command not found: '{' '.join(command_parts)}'.")
        if not suppress_errors:
            raise
    except subprocess.CalledProcessError as e:
        if not suppress_errors:
            logging.error(f"Command failed: '{' '.join(command_parts)}' Stderr: {e.stderr.strip()}")
            raise
    except Exception as e:
        if not suppress_errors:
            logging.error(f"Unexpected error running '{' '.join(command_parts)}': {e}")
            raise
    return ""

def get_system_dns_servers():
    """
    Returns actual upstream DNS servers.
    """
    try:
        from core.platform_utils import get_real_dns_servers
        return get_real_dns_servers()
    except ImportError:
        pass

    dns_servers = []
    # macOS specific DNS check via scutil
    if IS_MAC:
        try:
            out = _run_command(["scutil", "--dns"])
            for line in out.splitlines():
                if "nameserver" in line:
                    ip = line.split(":")[-1].strip()
                    if ip not in dns_servers and ip != "127.0.0.1":
                        dns_servers.append(ip)
        except: pass
    
    # Fallback to resolv.conf
    if not dns_servers:
        try:
            with open('/etc/resolv.conf', 'r') as f:
                for line in f:
                    if line.startswith('nameserver'):
                        parts = line.split()
                        if len(parts) > 1:
                            ip = parts[1]
                            if ip != '127.0.0.53': # Skip Linux stub
                                dns_servers.append(ip)
        except: pass
        
    return list(dict.fromkeys(dns_servers)) or ['1.1.1.1', '8.8.8.8']

def get_active_interfaces():
    """
    Retrieves detailed information for each active interface.
    """
    interfaces = []
    system_dns = get_system_dns_servers()

    if IS_MAC:
        # ---------------------------------------------------------
        # macOS LOGIC (Darwin)
        # ---------------------------------------------------------
        try:
            iface_list_out = _run_command(["ifconfig", "-l"])
            if not iface_list_out:
                return []
            
            all_ifaces = iface_list_out.strip().split()

            gateways_map = {}
            netstat_out = _run_command(["netstat", "-nr"])
            for line in netstat_out.splitlines():
                if "default" in line:
                    parts = line.split()
                    if len(parts) >= 4:
                        gw, if_name = parts[1], parts[3]
                        if if_name not in gateways_map:
                            gateways_map[if_name] = []
                        gateways_map[if_name].append(gw)

            for name in all_ifaces:
                details = _run_command(["ifconfig", name])
                if not details or "LOOPBACK" in details:
                    continue
                
                if "status: active" not in details and "UP" not in details:
                    continue

                interface_info = {
                    'name': name,
                    'flag': "UP",
                    'type': 'Wi-Fi' if name.startswith('en') else 'Ethernet',
                    'ip_addresses': [],
                    'mac': 'N/A',
                    'metric': 1,
                    'gateways': gateways_map.get(name, []),
                    'system_dns': system_dns
                }

                mac_match = re.search(r'ether\s+([0-9a-fA-F:]{17})', details)
                if mac_match:
                    interface_info['mac'] = mac_match.group(1).upper()

                ipv4s = re.findall(r'inet\s+(\d+\.\d+\.\d+\.\d+)', details)
                for ip in ipv4s:
                    interface_info['ip_addresses'].append(f"{ip}/24")

                interfaces.append(interface_info)
        except Exception as e:
            logging.error(f"macOS Interface detection failed: {e}")
        
        return interfaces

    else:
        # ---------------------------------------------------------
        # LINUX LOGIC (iproute2)
        # ---------------------------------------------------------
        try:
            ip_link_output = _run_command([_IP_CMD, '-o', 'link', 'show'])
            if not ip_link_output:
                return []

            for line in ip_link_output.splitlines():
                parts = line.split(':')
                if len(parts) < 2: continue
                name = parts[1].strip().split('@')[0]
                if name == 'lo' or name.startswith('veth'): continue

                interface_info = {
                    'name': name,
                    'flag': 'UP' if 'UP' in line else 'DOWN',
                    'type': 'Unknown',
                    'ip_addresses': [],
                    'mac': 'N/A',
                    'metric': 'N/A',
                    'gateways': [],
                    'system_dns': system_dns
                }

                mac_match = re.search(r'link/ether\s+([0-9a-fA-F:]{17})', line)
                if mac_match: interface_info['mac'] = mac_match.group(1).upper()

                # Get IPs
                ip_addr_output = _run_command([_IP_CMD, 'addr', 'show', name], suppress_errors=True)
                for ip_line in ip_addr_output.splitlines():
                    ip_match = re.search(r'inet\s+([0-9\./]+)', ip_line)
                    if ip_match: interface_info['ip_addresses'].append(ip_match.group(1))

                # Get Gateway/Metric
                ip_route_output = _run_command([_IP_CMD, 'route', 'show'], suppress_errors=True)
                for route_line in ip_route_output.splitlines():
                    if f"dev {name}" in route_line:
                        gw_match = re.search(r'via\s+([0-9\.]+)', route_line)
                        if gw_match: interface_info['gateways'].append(gw_match.group(1))

                interfaces.append(interface_info)
        except Exception as e:
            logging.error(f"Linux Interface detection failed: {e}")

        return interfaces

if __name__ == "__main__":
    print("--- Detected Network Interfaces ---")
    active_interfaces = get_active_interfaces()
    if not active_interfaces:
        print("No active network interfaces found.")
    else:
        for iface in active_interfaces:
            print(f"\nInterface: {iface['name']}")
            print(f"  Status: {iface['flag']} | Type: {iface['type']}")
            print(f"  IP Addresses: {', '.join(iface['ip_addresses'])}")
            print(f"  Gateways: {', '.join(iface['gateways'])}")