"""Tests for the root guard in intermux.__main__.main."""

from __future__ import annotations

import io
import unittest
from unittest import mock

from intermux import __main__ as entry


class RootGuardTests(unittest.TestCase):
    def test_cli_refused_as_root(self) -> None:
        err = io.StringIO()
        with mock.patch("os.geteuid", return_value=0), mock.patch("sys.stderr", err):
            self.assertEqual(entry.main(["report", "--save"]), 1)
        self.assertIn("run InterMux commands as your own user", err.getvalue())

    def test_gui_refused_as_root(self) -> None:
        with mock.patch("os.geteuid", return_value=0), mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(entry.main([]), 1)

    def test_helper_allowed_as_root(self) -> None:
        with mock.patch("os.geteuid", return_value=0), \
                mock.patch("intermux.helper.server.main", return_value=0) as helper:
            self.assertEqual(entry.main(["helper"]), 0)
        helper.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
