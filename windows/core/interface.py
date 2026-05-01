#!/usr/bin/env python3
"""
InterMux Windows — Network Interface Detection
Detects physical network adapters using psutil + PowerShell fallback.
Returns the same data shape as the Linux version for UI compatibility.
"""

import subprocess
import re
import logging
import json
from typing import List, Dict

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Virtual/irrelevant adapter name prefixes to exclude from the list
# ---------------------------------------------------------------------------
_VIRTUAL_PREFIXES = (
    "loopback",
    "pseudo",
    "isatap",
    "teredo",
    "6to4",
    "virtual",
    "hyper-v",
    "vmware",
    "virtualbox",
    "vbox",
    "tap",
    "wintun",
    "nordlynx",
    "mullvad",
    "openvpn",
    "vethernet",   # Hyper-V internal switch
    "bluetooth network connection",
)


def _is_virtual(name: str) -> bool:
    """Return True if the adapter name looks like a virtual/software-only adapter."""
    n = name.lower()
    return any(v in n for v in _VIRTUAL_PREFIXES)


# ---------------------------------------------------------------------------
# Primary: psutil-based detection
# ---------------------------------------------------------------------------

def _get_interfaces_psutil() -> List[Dict]:
    """
    Detect network interfaces using psutil (cross-version compatible).
    Returns a list of interface dicts matching the Linux data shape.
    """
    try:
        import psutil
    except ImportError:
        log.warning("psutil not installed — falling back to PowerShell detection.")
        return []

    interfaces = []
    stats   = psutil.net_if_stats()
    addrs   = psutil.net_if_addrs()
    gateways_map = _get_gateways_psutil(psutil)

    for name, addr_list in addrs.items():
        if _is_virtual(name):
            continue

        st = stats.get(name)
        is_up = st.isup if st else False

        ip_addresses = []
        mac = "N/A"

        import socket as _socket
        for addr in addr_list:
            if addr.family == _socket.AF_INET:
                # IPv4 with prefix length
                prefix = _cidr_from_mask(addr.netmask) if addr.netmask else "24"
                ip_addresses.append(f"{addr.address}/{prefix}")
            elif addr.family == _socket.AF_INET6:
                ip_addresses.append(addr.address)
            elif addr.family == psutil.AF_LINK:
                mac = addr.address.upper() if addr.address else "N/A"

        iface_type = _guess_type(name)

        interfaces.append({
            "name":        name,
            "flag":        "UP" if is_up else "DOWN",
            "type":        iface_type,
            "ip_addresses": ip_addresses,
            "mac":         mac,
            "metric":      "N/A",
            "gateways":    gateways_map.get(name, []),
            "system_dns":  get_system_dns_servers(),
        })

    return interfaces


def _get_gateways_psutil(psutil) -> Dict[str, List[str]]:
    """Returns {interface_name: [gateway_ip, ...]} using psutil.net_if_stats() + gateways()."""
    result: Dict[str, List[str]] = {}
    try:
        gw_info = psutil.net_if_stats()   # not the right call, use below
        all_gw  = psutil.net_gateways() if hasattr(psutil, "net_gateways") else {}
        import socket as _socket
        gw_data = psutil.net_default_gateway() if hasattr(psutil, "net_default_gateway") else {}
    except Exception:
        pass

    # Real call
    try:
        import psutil as _ps
        gateways = _ps.net_if_stats()   # Not gateways; use route parsing below
    except Exception:
        pass

    # Use PowerShell to get gateway per interface — more reliable
    return _get_gateways_powershell()


def _get_gateways_powershell() -> Dict[str, List[str]]:
    """
    Runs a PowerShell command to get default gateway per interface.
    Returns {interface_alias: [gateway_ip, ...]}
    """
    result: Dict[str, List[str]] = {}
    ps_cmd = (
        "Get-NetIPConfiguration | "
        "Select-Object -Property InterfaceAlias,"
        "@{Name='Gateway';Expression={$_.IPv4DefaultGateway.NextHop}} | "
        "ConvertTo-Json -Compress"
    )
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            text=True, stderr=subprocess.DEVNULL, timeout=10
        ).strip()
        if not out:
            return result
        data = json.loads(out)
        if isinstance(data, dict):
            data = [data]
        for entry in data:
            alias = entry.get("InterfaceAlias", "")
            gw    = entry.get("Gateway")
            if alias and gw:
                result.setdefault(alias, []).append(gw)
    except Exception as e:
        log.debug(f"PowerShell gateway lookup failed: {e}")
    return result


# ---------------------------------------------------------------------------
# Fallback: PowerShell-based detection
# ---------------------------------------------------------------------------

def _get_interfaces_powershell() -> List[Dict]:
    """
    Detect interfaces using Get-NetAdapter + Get-NetIPAddress via PowerShell.
    Used as a fallback when psutil is unavailable.
    """
    ps_cmd = (
        "Get-NetAdapter | "
        "Where-Object {$_.Status -ne 'NotPresent'} | "
        "Select-Object Name,Status,MacAddress,InterfaceDescription | "
        "ConvertTo-Json -Compress"
    )
    try:
        raw = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            text=True, stderr=subprocess.DEVNULL, timeout=15
        ).strip()
    except Exception as e:
        log.error(f"PowerShell adapter listing failed: {e}")
        return []

    if not raw:
        return []
    try:
        adapters = json.loads(raw)
        if isinstance(adapters, dict):
            adapters = [adapters]
    except json.JSONDecodeError as e:
        log.error(f"Failed to parse PowerShell JSON: {e}")
        return []

    gateways_map = _get_gateways_powershell()
    dns_servers  = get_system_dns_servers()
    interfaces   = []

    for adapter in adapters:
        name  = adapter.get("Name", "")
        desc  = adapter.get("InterfaceDescription", "")
        status = adapter.get("Status", "")
        mac   = adapter.get("MacAddress", "N/A") or "N/A"

        if _is_virtual(name) or _is_virtual(desc):
            continue

        is_up = status.lower() == "up"
        ip_addresses = _get_ips_for_adapter(name)
        iface_type   = _guess_type_from_desc(desc) or _guess_type(name)

        interfaces.append({
            "name":        name,
            "flag":        "UP" if is_up else "DOWN",
            "type":        iface_type,
            "ip_addresses": ip_addresses,
            "mac":         mac.replace("-", ":").upper(),
            "metric":      "N/A",
            "gateways":    gateways_map.get(name, []),
            "system_dns":  dns_servers,
        })

    return interfaces


def _get_ips_for_adapter(adapter_name: str) -> List[str]:
    """Returns list of IP/prefix strings for a given adapter name."""
    ps_cmd = (
        f"Get-NetIPAddress -InterfaceAlias '{adapter_name}' "
        f"-AddressFamily IPv4 | "
        f"Select-Object IPAddress,PrefixLength | ConvertTo-Json -Compress"
    )
    try:
        raw = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            text=True, stderr=subprocess.DEVNULL, timeout=8
        ).strip()
        if not raw:
            return []
        data = json.loads(raw)
        if isinstance(data, dict):
            data = [data]
        return [f"{d['IPAddress']}/{d['PrefixLength']}" for d in data
                if d.get("IPAddress") and not d["IPAddress"].startswith("169.254")]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_active_interfaces() -> List[Dict]:
    """
    Returns a list of network interface dicts.
    Tries psutil first, falls back to PowerShell.
    Same data shape as Linux core/interface.py.
    """
    ifaces = _get_interfaces_psutil()
    if not ifaces:
        log.info("psutil detection returned nothing — trying PowerShell")
        ifaces = _get_interfaces_powershell()

    log.debug(f"Detected {len(ifaces)} interfaces: {[i['name'] for i in ifaces]}")
    return ifaces


def get_system_dns_servers() -> List[str]:
    """
    Returns upstream DNS servers by parsing 'ipconfig /all'.
    Skips loopback addresses.
    """
    servers = []
    try:
        out = subprocess.check_output(
            ["ipconfig", "/all"], text=True, stderr=subprocess.DEVNULL, encoding="utf-8", errors="replace"
        )
        for line in out.splitlines():
            line = line.strip()
            if "DNS Servers" in line or (servers and line and re.match(r'^\d{1,3}\.', line)):
                ips = re.findall(r'\b\d{1,3}(?:\.\d{1,3}){3}\b', line)
                for ip in ips:
                    if not ip.startswith("127.") and ip not in servers:
                        servers.append(ip)
    except Exception as e:
        log.warning(f"DNS detection failed: {e}")
    return servers or ["1.1.1.1", "8.8.8.8"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cidr_from_mask(mask: str) -> str:
    """Convert dotted subnet mask to CIDR prefix length string."""
    try:
        import ipaddress
        return str(ipaddress.IPv4Network(f"0.0.0.0/{mask}").prefixlen)
    except Exception:
        return "24"


def _guess_type(name: str) -> str:
    n = name.lower()
    if any(k in n for k in ("wi-fi", "wifi", "wlan", "wireless")):
        return "Wi-Fi"
    if any(k in n for k in ("ethernet", "eth", "lan", "local area")):
        return "Ethernet"
    if "usb" in n or "tether" in n:
        return "USB Tethering"
    if "bluetooth" in n:
        return "Bluetooth Tethering"
    return "Unknown"


def _guess_type_from_desc(desc: str) -> str:
    d = desc.lower()
    if any(k in d for k in ("wireless", "wi-fi", "802.11", "wlan")):
        return "Wi-Fi"
    if any(k in d for k in ("ethernet", "gigabit", "realtek", "intel(r) ethernet")):
        return "Ethernet"
    if "usb" in d or "rndis" in d:
        return "USB Tethering"
    if "bluetooth" in d and "pan" in d:
        return "Bluetooth Tethering"
    return ""


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    print("--- Detected Network Interfaces ---")
    for iface in get_active_interfaces():
        print(f"\nInterface : {iface['name']}")
        print(f"  Status  : {iface['flag']}")
        print(f"  Type    : {iface['type']}")
        print(f"  IPs     : {', '.join(iface['ip_addresses']) or 'N/A'}")
        print(f"  Gateways: {', '.join(iface['gateways']) or 'N/A'}")
        print(f"  DNS     : {', '.join(iface['system_dns'])}")
