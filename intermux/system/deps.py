"""Distribution detection and check for the external tools InterMux needs."""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from intermux.system import commands

log = logging.getLogger("intermux.system.deps")

_PM_BY_DISTRO = {
    "apt": ("debian", "ubuntu", "linuxmint", "pop", "elementary", "kali"),
    "pacman": ("arch", "manjaro", "endeavouros", "garuda"),
    "dnf": ("fedora", "rhel", "centos", "rocky", "almalinux", "nobara"),
    "zypper": ("opensuse", "suse", "sles"),
    "apk": ("alpine",),
}
_PM_PROBE_ORDER = ("apt", "pacman", "dnf", "zypper", "apk")
_INSTALL_CMD = {
    "apt": "sudo apt install -y",
    "pacman": "sudo pacman -S",
    "dnf": "sudo dnf install -y",
    "zypper": "sudo zypper install",
    "apk": "sudo apk add",
}

# policykit-1 is a transitional package on Debian 13 / Ubuntu 24.04+ (it pulls in pkexec and polkitd) and the
# only name on older releases, so it installs everywhere on apt systems.
# (tool, required, purpose, default package, per-package-manager package overrides)
_TOOL_SPECS: Tuple[Tuple[str, bool, str, str, Dict[str, str]], ...] = (
    ("ip", True, "network namespaces and routing", "iproute2", {"dnf": "iproute"}),
    # iptables is the iptables-nft front end on current distros; Fedora/RHEL name the package explicitly.
    ("iptables", True, "NAT for namespaces", "iptables", {"dnf": "iptables-nft"}),
    # sysctl (procps) is run inside the namespace for the DNS redirect (route_localnet).
    ("sysctl", True, "namespace DNS redirect", "procps",
     {"pacman": "procps-ng", "dnf": "procps-ng", "apk": "procps-ng"}),
    ("runuser", True, "launch apps as your user", "util-linux", {}),
    ("pkexec", True, "graphical privilege prompt", "policykit-1",
     {"pacman": "polkit", "dnf": "polkit", "zypper": "polkit", "apk": "polkit"}),
    ("curl", False, "public IP verification", "curl", {}),
    ("nft", False, "route an already running app (shift current session)", "nftables", {}),
    ("xdg-open", False, "open links and folders", "xdg-utils", {}),
)


@dataclass
class DistroInfo:
    id: str
    id_like: List[str]
    name: str
    version_id: str
    package_manager: str          # "apt" | "pacman" | "dnf" | "zypper" | "apk" | "unknown"


@dataclass
class Dependency:
    tool: str
    required: bool
    present: bool
    purpose: str
    install_hint: str


def _parse_os_release(paths: Sequence[str]) -> Dict[str, str]:
    for path in paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError:
            continue
        data: Dict[str, str] = {}
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            data[key.strip()] = value
        return data
    return {}


def _package_manager(distro_id: str, id_like: Sequence[str]) -> str:
    for token in [distro_id] + list(id_like):
        for manager, ids in _PM_BY_DISTRO.items():
            if token in ids:
                return manager
    for manager in _PM_PROBE_ORDER:
        if shutil.which(manager):
            return manager
    return "unknown"


def distro_info(os_release_paths: Sequence[str] = ("/etc/os-release", "/usr/lib/os-release")) -> DistroInfo:
    """Parse os-release and determine the package manager family."""
    data = _parse_os_release(os_release_paths)
    distro_id = data.get("ID", "").lower()
    id_like = data.get("ID_LIKE", "").lower().split()
    return DistroInfo(
        id=distro_id,
        id_like=id_like,
        name=data.get("NAME", ""),
        version_id=data.get("VERSION_ID", ""),
        package_manager=_package_manager(distro_id, id_like),
    )


def _install_hint(manager: str, package: str) -> str:
    if manager in _INSTALL_CMD:
        return f"{_INSTALL_CMD[manager]} {package}"
    return f"install {package} with your package manager"


def check_dependencies(distro: Optional[DistroInfo] = None) -> List[Dependency]:
    """Report each external tool InterMux uses, whether it is present, and how to install it."""
    if distro is None:
        distro = distro_info()
    manager = distro.package_manager
    deps: List[Dependency] = []
    for tool, required, purpose, default_pkg, overrides in _TOOL_SPECS:
        package = overrides.get(manager, default_pkg)
        deps.append(Dependency(
            tool=tool,
            required=required,
            present=commands.which(tool) is not None,
            purpose=purpose,
            install_hint=_install_hint(manager, package),
        ))
    return deps


def missing_required(distro: Optional[DistroInfo] = None) -> List[Dependency]:
    """Return the required tools that are not installed."""
    return [dep for dep in check_dependencies(distro) if dep.required and not dep.present]
