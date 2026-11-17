"""The Python and Rust parsers share these input fixtures and expected answers."""
import json
import unittest
from pathlib import Path

from intermux.system.netinfo import parse_ip_json
from intermux.state import state_from_dict


FIXTURES = Path(__file__).parent / "fixtures"


def read(name):
    return json.loads((FIXTURES / name).read_text())


class SharedFixtureTests(unittest.TestCase):
    def test_interfaces(self):
        interfaces = parse_ip_json(read("ip-link.json"), read("ip-addr.json"),
                                   read("ip-route.json"), FIXTURES / "sys-class-net")
        actual = [dict(i.to_dict(), ipv4=i.ipv4_cidrs, bindable=i.bindable) for i in interfaces]
        self.assertEqual(actual, read("expected-interfaces.json"))

    def test_current_and_old_state(self):
        current = state_from_dict(read("state-current.json"))
        old = state_from_dict(read("state-old.json"))
        self.assertEqual(current.bindings[0].uid, 1000)
        self.assertIsNone(old.bindings[0].uid)
        self.assertIsNone(old.bindings[0].pid)
        self.assertEqual(old.adoptions[0].origins, {})

    def test_malformed_entries_are_skipped(self):
        with self.assertLogs("intermux.state", level="WARNING"):
            mixed = state_from_dict(read("state-mixed.json"))
        self.assertEqual(mixed, state_from_dict(read("state-current.json")))

    def test_invalid_state(self):
        with self.assertRaises(ValueError):
            state_from_dict(read("state-invalid.json"))
        with self.assertRaises(ValueError):
            read("state-partial.json")
