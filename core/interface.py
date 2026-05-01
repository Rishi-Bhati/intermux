# intermux/core/interface.py

import subprocess
import re
import logging
import shutil

# Configure logging for better error reporting and debugging
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

# Locate the 'ip' binary — path differs across distros (/sbin, /usr/sbin, /bin)
_IP_CMD = shutil.which("ip") or "/usr/sbin/ip"

def _run_command(command_parts, check_return=True, suppress_errors=False):
    """
    Helper function to run a shell command and capture its output.

    Args:
        command_parts (list): A list of strings representing the command and its arguments.
                              E.g., ['ip', '-o', 'link', 'show']
        check_return (bool): If True, raise an exception if the command returns a non-zero exit code.
        suppress_errors (bool): If True, log errors but do not raise an exception.

    Returns:
        str: The standard output of the command.

    Raises:
        subprocess.CalledProcessError: If the command returns a non-zero exit code and check_return is True.
        FileNotFoundError: If the command itself is not found.
    """
    try:
        result = subprocess.run(
            command_parts,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,  # Capture stderr to log potential issues
            text=True,
            check=check_return,
            encoding='utf-8' # Ensure consistent encoding
        )
        return result.stdout.strip()
    except FileNotFoundError:
        logging.error(f"Command not found: '{' '.join(command_parts)}'. Make sure it's in your PATH.")
        if not suppress_errors:
            raise
    except subprocess.CalledProcessError as e:
        logging.error(f"Command failed: '{' '.join(command_parts)}'")
        logging.error(f"Stderr: {e.stderr.strip()}")
        if not suppress_errors:
            raise
    except Exception as e:
        logging.error(f"An unexpected error occurred while running '{' '.join(command_parts)}': {e}")
        if not suppress_errors:
            raise
    return "" # Return empty string on error if suppressed

def get_system_dns_servers():
    """
    Returns actual upstream DNS servers.
    Delegates to platform_utils which handles systemd-resolved stubs correctly
    across all distros (Arch, Ubuntu, Fedora, etc.).

    Returns:
        list: A list of IP address strings.
    """
    try:
        from core.platform_utils import get_real_dns_servers
        return get_real_dns_servers()
    except ImportError:
        pass

    # Fallback: read resolv.conf directly
    dns_servers = []
    try:
        with open('/etc/resolv.conf', 'r') as f:
            for line in f:
                line = line.strip()
                if line.startswith('nameserver'):
                    parts = line.split()
                    if len(parts) > 1:
                        ip = parts[1]
                        if re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', ip) or \
                           re.match(r'^([0-9a-fA-F]{1,4}:){1,7}[0-9a-fA-F]{1,4}$', ip):
                            if ip != '127.0.0.53':
                                dns_servers.append(ip)
    except FileNotFoundError:
        logging.warning("/etc/resolv.conf not found.")
    except Exception as e:
        logging.error(f"Error reading /etc/resolv.conf: {e}")
    return dns_servers or ['1.1.1.1', '8.8.8.8']

def get_active_interfaces():
    """
    Connects to all available internet interfaces (Wi-Fi, LAN, USB, Bluetooth tethering)
    and retrieves detailed information for each active interface on a Linux system.

    Uses 'ip' command-line utility for information gathering.

    Returns:
        list: A list of dictionaries, where each dictionary represents an active network
              interface with its name, status flag, detected type, IP addresses (IPv4 & IPv6),
              MAC address, metric, and associated gateways.
    """
    interfaces = []
    system_dns = get_system_dns_servers() # Get system-wide DNS once

    # 1. Get basic link information for all interfaces
    ip_link_output = _run_command([_IP_CMD, '-o', 'link', 'show'])
    if not ip_link_output:
        logging.error("Failed to get basic interface link information. Is iproute2 installed?")
        return []

    link_lines = ip_link_output.strip().split('\n')

    for line in link_lines:
        parts = line.split(':')
        if len(parts) < 2:
            continue

        name = parts[1].strip().split('@')[0]
        
        # Skip loopback and veth interfaces
        if name == 'lo' or name.startswith('veth'):
            continue

        interface_info = {
            'name': name,
            'flag': 'DOWN',  # Default to DOWN, update if UP found
            'type': 'Unknown',
            'ip_addresses': [],
            'mac': 'N/A',
            'metric': 'N/A',
            'gateways': [],
            'system_dns': system_dns # Add system-wide DNS to each interface's info
        }

        # Extract flags (e.g., <UP,BROADCAST,RUNNING,MULTICAST>)
        flags_match = re.search(r'<([^>]+)>', line)
        if flags_match:
            flags_str = flags_match.group(1)
            if "UP" in flags_str:
                interface_info['flag'] = "UP"
        
        # Only process active interfaces for detailed information
        if interface_info['flag'] == "DOWN":
            interfaces.append(interface_info)
            continue # Skip detailed info for down interfaces

        # Get MAC Address (usually the last part of the 'ip link show' output for 'link/ether')
        mac_match = re.search(r'link/ether\s+([0-9a-fA-F:]{17})', line)
        if mac_match:
            interface_info['mac'] = mac_match.group(1).upper()
        
        # Determine interface type
        if name.startswith("wl"):
            interface_info['type'] = "Wi-Fi"
        elif name.startswith("en") or name.startswith("eth"):
            interface_info['type'] = "Ethernet"
        elif name.startswith("usb"):
            interface_info['type'] = "USB"
        elif name.startswith("bnep") or name.startswith("bt"):
            interface_info['type'] = "Bluetooth Tethering"
        elif name.startswith("veth") or name.startswith("br") or \
             name.startswith("docker") or name.startswith("tun") or \
             name.startswith("tap"):
            interface_info['type'] = "Virtual/Bridge/VPN"
        
        # 2. Get IP Addresses (IPv4 and IPv6) with CIDR
        for family in ['inet', 'inet6']:
            ip_addr_output = _run_command([_IP_CMD, '-f', family, 'addr', 'show', name], suppress_errors=True)
            if ip_addr_output:
                for ip_line in ip_addr_output.split('\n'):
                    # Regex to find 'inet X.X.X.X/YY' or 'inet6 XXXX::/YY'
                    ip_match = re.search(r'inet(?:6)?\s+([0-9a-fA-F.:/]+)\s+brd', ip_line)
                    if not ip_match:
                         ip_match = re.search(r'inet(?:6)?\s+([0-9a-fA-F.:/]+)\s+scope', ip_line)
                    if ip_match:
                        interface_info['ip_addresses'].append(ip_match.group(1))

        # 3. Get Routes, Metric, and Gateways
        ip_route_output = _run_command([_IP_CMD, 'route', 'show'], suppress_errors=True)
        if ip_route_output:
            for route_line in ip_route_output.split('\n'):
                if f"dev {name}" in route_line:
                    # Extract metric
                    metric_match = re.search(r'metric\s+(\d+)', route_line)
                    if metric_match:
                        interface_info['metric'] = int(metric_match.group(1))
                    
                    # Extract gateway (via) for routes specific to this interface
                    gateway_match = re.search(r'via\s+([0-9a-fA-F.:]+)', route_line)
                    if gateway_match and gateway_match.group(1) not in interface_info['gateways']:
                        interface_info['gateways'].append(gateway_match.group(1))

        interfaces.append(interface_info)
        # print(f"Detected interface: {interfaces}")

    return interfaces

if __name__ == "__main__":
    print("--- Detected Network Interfaces ---")
    active_interfaces = get_active_interfaces()
    if not active_interfaces:
        print("No active network interfaces found or an error occurred.")
    else:
        for iface in active_interfaces:
            print(f"\nInterface: {iface['name']}")
            print(f"  Status: {iface['flag']}")
            print(f"  Type: {iface['type']}")
            print(f"  MAC Address: {iface['mac']}")
            print(f"  IP Addresses: {', '.join(iface['ip_addresses']) if iface['ip_addresses'] else 'N/A'}")
            print(f"  Metric: {iface['metric']}")
            print(f"  Gateways (associated with device routes): {', '.join(iface['gateways']) if iface['gateways'] else 'N/A'}")
            print(f"  System DNS Servers: {', '.join(iface['system_dns']) if iface['system_dns'] else 'N/A'}")

    print("\n--- End of Report ---")