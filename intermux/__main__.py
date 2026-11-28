"""Top-level dispatch: GUI with no arguments, privileged helper for "helper", CLI otherwise."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional


def _native_gui() -> Optional[Path]:
    if getattr(sys, "frozen", False):
        candidates = [Path(sys.executable).resolve().with_name("intermux-ui")]
    else:
        source = Path(__file__).resolve().parents[1] / "rust" / "intermux-ui"
        candidates = [source / "target" / profile / "intermux-ui" for profile in ("release", "debug")]
    return next((path for path in candidates if path.is_file() and os.access(path, os.X_OK)), None)


def _raise_gui() -> bool:
    from intermux import daemon_client

    socket = daemon_client.socket_path()
    if not socket or not os.path.exists(socket):
        return False
    client = daemon_client.DaemonClient("gui", path=socket)
    token = os.environ.get("XDG_ACTIVATION_TOKEN") or os.environ.get("DESKTOP_STARTUP_ID")
    try:
        client.connect(timeout=0.5)
        result = client.request("gui.raise", {"activation_token": token} if token else {}, timeout=0.5)
        return isinstance(result, dict) and result.get("raised") is True
    except (OSError, ValueError, daemon_client.DaemonError):
        return False
    finally:
        client.close()


def _launch_gui() -> int:
    mode = os.environ.get("INTERMUX_GUI", "auto")
    if mode not in {"auto", "rune", "tk"}:
        print("[X] INTERMUX_GUI must be auto, rune, or tk", file=sys.stderr)
        return 1
    native = None if mode == "tk" else _native_gui()
    if native is not None:
        if _raise_gui():
            return 0
        env = os.environ.copy()
        if getattr(sys, "frozen", False):
            # The native process needs system libraries. Start a fresh bootloader for its bridge.
            original = env.pop("LD_LIBRARY_PATH_ORIG", None)
            if original is None:
                env.pop("LD_LIBRARY_PATH", None)
            else:
                env["LD_LIBRARY_PATH"] = original
            env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
            env["INTERMUX_GUI_BRIDGE"] = str(Path(sys.executable).resolve())
        else:
            env.pop("INTERMUX_GUI_BRIDGE", None)
            env["INTERMUX_GUI_BRIDGE_PYTHON"] = sys.executable
            env["INTERMUX_GUI_BRIDGE_CWD"] = str(Path(__file__).resolve().parents[1])
        try:
            status = subprocess.run([str(native)], env=env).returncode
        except OSError as exc:
            print(f"[X] Could not start InterMux Rune: {exc}", file=sys.stderr)
            status = 75
        if status != 75 or mode == "rune":
            return status if status >= 0 else 128 - status
        print("[!] InterMux Rune could not start. Opening the Tk interface.", file=sys.stderr)
    elif mode == "rune":
        print("[X] The intermux-ui executable is missing. Build it or install a bundle with Rune.", file=sys.stderr)
        return 1

    from intermux.ui.app import main as gui_main

    return gui_main()


def main(argv: Optional[List[str]] = None) -> int:
    """Dispatch argv to the GUI, the helper, or the CLI and return the exit code."""
    if argv is None:
        argv = sys.argv[1:]
    # polkit authorises the whole binary, so root may only ever run the helper, watcher, or passwordless.
    if os.geteuid() == 0 and argv[:1] not in (["helper"], ["watch"], ["passwordless"]):
        if argv:
            sys.stderr.write("[X] run InterMux commands as your own user; root is used only by the helper\n")
        else:
            sys.stderr.write("[X] the GUI cannot run as root; run InterMux as your own user\n")
        return 1
    # The watcher is a long-running root service for systemd; pkexec must only ever start the helper.
    if argv[:1] == ["watch"] and "PKEXEC_UID" in os.environ:
        sys.stderr.write("[X] the watcher runs as a system service (intermux-watch.service), not through pkexec\n")
        return 1
    # With passwordless on, any program can run pkexec without a password, so the helper's pkexec action must
    # never reach the passwordless command. Only the intermux-passwordless script may: its own polkit action
    # always asks for an administrator password, and pkexec's cleared environment lets only it set the marker.
    if (argv[:1] == ["passwordless"] and "PKEXEC_UID" in os.environ
            and os.environ.get("INTERMUX_PASSWORDLESS_ACTION") != "1"):
        sys.stderr.write("[X] passwordless can only be changed with sudo, not through pkexec\n")
        return 1
    # Imports are lazy so that `intermux helper` never imports tkinter.
    if not argv:
        return _launch_gui()
    if argv == ["gui-bridge"]:
        from intermux.gui_bridge import main as bridge_main

        return bridge_main()
    if argv[0] == "helper":
        from intermux.helper.server import main as helper_main

        return helper_main()
    if argv[0] == "watch":
        from intermux.watch import main as watch_main

        return watch_main()
    from intermux.cli import main as cli_main

    return cli_main(argv)


if __name__ == "__main__":
    sys.exit(main())
