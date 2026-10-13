"""Dependency table checks."""

from __future__ import annotations

import unittest
from unittest import mock

from intermux.system import deps


def _distro(manager: str) -> deps.DistroInfo:
    return deps.DistroInfo(id="x", id_like=[], name="x", version_id="", package_manager=manager)


class ToolTableTests(unittest.TestCase):
    def _by_tool(self, manager: str):
        with mock.patch.object(deps.commands, "which", return_value=None):
            return {d.tool: d for d in deps.check_dependencies(_distro(manager))}

    def test_sysctl_replaces_pgrep_and_is_required(self) -> None:
        table = self._by_tool("apt")
        self.assertNotIn("pgrep", table)
        self.assertTrue(table["sysctl"].required)

    def test_sysctl_package_names(self) -> None:
        self.assertTrue(self._by_tool("apt")["sysctl"].install_hint.endswith(" procps"))
        self.assertTrue(self._by_tool("pacman")["sysctl"].install_hint.endswith(" procps-ng"))
        self.assertTrue(self._by_tool("dnf")["sysctl"].install_hint.endswith(" procps-ng"))

    def test_iptables_nft_on_dnf(self) -> None:
        self.assertTrue(self._by_tool("dnf")["iptables"].install_hint.endswith(" iptables-nft"))
        self.assertTrue(self._by_tool("apt")["iptables"].install_hint.endswith(" iptables"))

    def test_missing_required_reports_sysctl(self) -> None:
        with mock.patch.object(deps.commands, "which", return_value=None):
            names = [d.tool for d in deps.missing_required(_distro("apt"))]
        self.assertIn("sysctl", names)


if __name__ == "__main__":
    unittest.main()
