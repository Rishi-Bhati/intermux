"""Root-only integration test for namespace plumbing and the engine lifecycle.

Runs only when INTERMUX_INTEGRATION=1 and the effective uid is 0. It creates a
dummy interface, binds /bin/sleep to it, and tears everything down again. Run with:

    sudo -E env INTERMUX_INTEGRATION=1 python3 -m unittest tests.test_netns_integration -v
"""

from __future__ import annotations

import dataclasses
import logging
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import List

from intermux import config, engine, state
from intermux.errors import NotFoundError, SystemCommandError
from intermux.system import commands, netns, procs
from intermux.system.commands import CmdResult
from intermux.system.session import user_for_uid

log = logging.getLogger("intermux.tests.integration")

IFACE = "ilxtest0"
IFACE_CIDR = "10.99.7.2/24"
IFACE_GATEWAY = "10.99.7.1"
APP = "/bin/sleep"
NOBODY_UID = 65534
IP = commands.which("ip") or "/usr/sbin/ip"

_ENABLED = (
    os.environ.get("INTERMUX_INTEGRATION") == "1"
    and hasattr(os, "geteuid")
    and os.geteuid() == 0
)


def _ip(*args: str, allow_exists: bool = False) -> CmdResult:
    """Run an ip command; raise SystemCommandError on failure unless 'File exists' is allowed."""
    res = commands.run([IP, *args])
    if res.ok:
        return res
    if allow_exists and "File exists" in res.stderr:
        return res
    raise SystemCommandError(f"ip {' '.join(args)} failed: {res.stderr.strip()}", result=res)


@unittest.skipUnless(_ENABLED, "requires INTERMUX_INTEGRATION=1 and root")
class NetnsIntegrationTest(unittest.TestCase):

    def setUp(self) -> None:
        try:
            base_user = user_for_uid(NOBODY_UID)
        except NotFoundError:
            self.skipTest(f"uid {NOBODY_UID} (nobody) not present")
        if not os.path.exists("/bin/sh") or not os.path.exists("/bin/sleep"):
            self.skipTest("/bin/sh or /bin/sleep not present")

        self._patched = False
        self._state_dirs: List[str] = []
        self._orig_defaults: dict = {}
        self._orig_paths: tuple = ()

        home = tempfile.mkdtemp(prefix="intermux-itest-home-")
        self._state_dirs.append(home)
        os.chmod(home, 0o755)
        # A named shell script keeps the app alive (sleep needs an argument, so the bare
        # /bin/sleep exits at once). The script's process is named "ilxsleep" for matching.
        app_dir = tempfile.mkdtemp(prefix="intermux-itest-app-")
        self._state_dirs.append(app_dir)
        os.chmod(app_dir, 0o755)
        self.app = os.path.join(app_dir, "ilxsleep")
        with open(self.app, "w", encoding="utf-8") as script:
            script.write("#!/bin/sh\nsleep 300\n")
        os.chmod(self.app, 0o755)
        # Use a throwaway home so the engine's log directory is not created under the real nobody home.
        self.user = dataclasses.replace(base_user, home=home)

        state_dir = tempfile.mkdtemp(prefix="intermux-itest-state-")
        self._state_dirs.append(state_dir)
        self._patch_state_paths(Path(state_dir) / "state.json")

        try:
            self._create_dummy()
        except BaseException:
            self._cleanup()
            raise

    def tearDown(self) -> None:
        self._cleanup()

    def test_plumbing(self) -> None:
        ns = netns.namespace_name(IFACE)

        result = engine.bind(self.app, IFACE, user=self.user, env={}, running_policy="separate")
        self.assertIn("binding", result)
        self.assertTrue(netns.namespace_exists(ns), "namespace was not created")
        self.assertTrue(procs.pids_in_netns(ns), "no process started inside the namespace")
        self.assertIsNotNone(state.load_state().find_binding(IFACE, self.app), "binding not persisted")

        statuses = engine.status()
        entry = next((s for s in statuses if s["iface"] == IFACE and s["app"] == self.app), None)
        self.assertIsNotNone(entry, "binding missing from engine.status()")
        self.assertTrue(entry["running_pids"], "engine.status() reports no running pids")

        stopped = engine.stop_app(IFACE, self.app, uid=self.user.uid)
        self.assertEqual(stopped["still_running"], [])

        engine.teardown_idle()
        self.assertFalse(netns.namespace_exists(ns), "namespace still exists after teardown_idle")
        self.assertEqual(state.load_state().bindings, [])

    # helpers

    def _create_dummy(self) -> None:
        _ip("link", "add", IFACE, "type", "dummy", allow_exists=True)
        _ip("addr", "add", IFACE_CIDR, "dev", IFACE, allow_exists=True)
        _ip("link", "set", IFACE, "up")
        _ip("route", "add", "default", "via", IFACE_GATEWAY, "dev", IFACE, "metric", "9000",
            allow_exists=True)

    def _patch_state_paths(self, path: Path) -> None:
        # load_state/save_state bind STATE_FILE as a default argument at definition time, so the
        # module attribute alone is not enough; the defaults tuple is swapped as well.
        funcs = (state.load_state, state.save_state)
        self._orig_defaults = {f: f.__defaults__ for f in funcs}
        self._orig_paths = (config.STATE_FILE, state.STATE_FILE)
        for f in funcs:
            f.__defaults__ = (path,)
        config.STATE_FILE = path
        state.STATE_FILE = path
        self._patched = True

    def _restore_state_paths(self) -> None:
        if not self._patched:
            return
        for f, defaults in self._orig_defaults.items():
            f.__defaults__ = defaults
        config.STATE_FILE, state.STATE_FILE = self._orig_paths
        self._patched = False

    def _cleanup(self) -> None:
        # Must never raise: runs from tearDown and from a failed setUp.
        try:
            uid = getattr(getattr(self, "user", None), "uid", None)
            engine.reset(kill_apps=True, uid=uid)
        except Exception:
            log.warning("integration cleanup: engine.reset failed", exc_info=True)
        try:
            commands.run([IP, "link", "del", IFACE])
        except Exception:
            log.warning("integration cleanup: deleting %s failed", IFACE, exc_info=True)
        try:
            self._restore_state_paths()
        except Exception:
            log.warning("integration cleanup: restoring state paths failed", exc_info=True)
        for d in getattr(self, "_state_dirs", []):
            shutil.rmtree(d, ignore_errors=True)
        self._state_dirs = []


if __name__ == "__main__":
    unittest.main()
