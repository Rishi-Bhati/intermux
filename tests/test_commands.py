from __future__ import annotations

import logging
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

from intermux.errors import SystemCommandError
from intermux.system import commands

PY = sys.executable
LOGGER = "intermux.system.commands"


class RunTests(unittest.TestCase):
    def test_success_captures_stdout(self):
        res = commands.run([PY, "-c", "print('hi')"])
        self.assertTrue(res.ok)
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout, "hi\n")
        self.assertEqual(res.argv, [PY, "-c", "print('hi')"])

    def test_nonzero_exit_is_not_raised(self):
        res = commands.run([PY, "-c", "import sys; sys.exit(3)"])
        self.assertEqual(res.returncode, 3)
        self.assertFalse(res.ok)

    def test_missing_binary(self):
        res = commands.run(["intermux-no-such-binary-xyz"])
        self.assertEqual(res.returncode, 127)
        self.assertIn("command not found: intermux-no-such-binary-xyz", res.stderr)

    def test_timeout_returns_124(self):
        res = commands.run([PY, "-c", "import time; time.sleep(10)"], timeout=0.5)
        self.assertEqual(res.returncode, 124)
        self.assertFalse(res.ok)

    def test_input_text_is_sent_to_stdin(self):
        code = "import sys; sys.stdout.write(sys.stdin.read().upper())"
        res = commands.run([PY, "-c", code], input_text="abc")
        self.assertEqual(res.stdout, "ABC")

    def test_stdin_is_empty_without_input(self):
        res = commands.run([PY, "-c", "import sys; print(repr(sys.stdin.read()))"])
        self.assertEqual(res.stdout, "''\n")

    def test_env_replaces_environment(self):
        code = "import os; print(os.environ.get('INTERMUX_T'), 'HOME' in os.environ)"
        res = commands.run([PY, "-c", code], env={"INTERMUX_T": "42"})
        self.assertEqual(res.stdout, "42 False\n")

    def test_env_none_inherits_environment(self):
        key = "INTERMUX_TEST_INHERIT"
        os.environ[key] = "yes"
        try:
            res = commands.run([PY, "-c", f"import os; print(os.environ.get('{key}'))"])
        finally:
            del os.environ[key]
        self.assertEqual(res.stdout, "yes\n")

    def test_argv_must_be_list_of_str(self):
        with self.assertRaises(TypeError):
            commands.run("true")
        with self.assertRaises(TypeError):
            commands.run(["true", 1])
        with self.assertRaises(TypeError):
            commands.run(None)

    def test_tuple_argv_accepted(self):
        res = commands.run((PY, "-c", "print(1)"))
        self.assertEqual(res.stdout, "1\n")

    def test_failure_logs_warning(self):
        with self.assertLogs(LOGGER, level="WARNING"):
            commands.run([PY, "-c", "import sys; sys.exit(2)"])

    def test_tolerated_failure_logs_debug_only(self):
        code = "import sys; sys.stderr.write('Cannot find device'); sys.exit(1)"
        with self.assertLogs(LOGGER, level="DEBUG") as cm:
            res = commands.run([PY, "-c", code], tolerate=["Cannot find device"])
        self.assertEqual(res.returncode, 1)
        self.assertTrue(all(rec.levelno == logging.DEBUG for rec in cm.records))


class RunCheckedTests(unittest.TestCase):
    def test_success_returns_result(self):
        res = commands.run_checked([PY, "-c", "print('ok')"])
        self.assertEqual(res.stdout, "ok\n")

    def test_failure_raises_with_result(self):
        code = "import sys; sys.stderr.write('boom'); sys.exit(4)"
        with self.assertRaises(SystemCommandError) as cm:
            commands.run_checked([PY, "-c", code])
        self.assertEqual(cm.exception.result.returncode, 4)
        self.assertIn("boom", str(cm.exception))

    def test_missing_binary_raises(self):
        with self.assertRaises(SystemCommandError) as cm:
            commands.run_checked(["intermux-no-such-binary-xyz"])
        self.assertEqual(cm.exception.result.returncode, 127)

    def test_tolerated_failure_does_not_raise(self):
        code = "import sys; sys.stderr.write('Cannot find device'); sys.exit(1)"
        res = commands.run_checked([PY, "-c", code], tolerate=["Cannot find device"])
        self.assertEqual(res.returncode, 1)

    def test_untolerated_stderr_still_raises(self):
        code = "import sys; sys.stderr.write('other problem'); sys.exit(1)"
        with self.assertRaises(SystemCommandError):
            commands.run_checked([PY, "-c", code], tolerate=["Cannot find device"])


class JsonCmdTests(unittest.TestCase):
    def test_parses_json(self):
        self.assertEqual(commands.json_cmd([PY, "-c", "print('[1, 2]')"]), [1, 2])

    def test_invalid_json_returns_empty_list(self):
        with self.assertLogs(LOGGER, level="WARNING"):
            self.assertEqual(commands.json_cmd([PY, "-c", "print('not json')"]), [])

    def test_failure_returns_empty_list(self):
        self.assertEqual(commands.json_cmd([PY, "-c", "import sys; sys.exit(1)"]), [])


class WhichTests(unittest.TestCase):
    def test_finds_common_tool(self):
        self.assertIsNotNone(commands.which("sh"))

    def test_missing_tool_is_none(self):
        self.assertIsNone(commands.which("intermux-no-such-binary-xyz"))


    def test_fallback_dir_used_when_not_on_path(self):
        with tempfile.TemporaryDirectory() as d:
            exe = os.path.join(d, "intermux-fake-tool")
            with open(exe, "w") as fh:
                fh.write("#!/bin/sh\n")
            os.chmod(exe, os.stat(exe).st_mode | stat.S_IXUSR)
            with mock.patch.object(commands, "FALLBACK_DIRS", (d,)):
                self.assertEqual(commands.which("intermux-fake-tool"), exe)

    def test_missing_everywhere_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(commands, "FALLBACK_DIRS", (d,)):
                self.assertIsNone(commands.which("intermux-fake-tool"))


if __name__ == "__main__":
    unittest.main()
