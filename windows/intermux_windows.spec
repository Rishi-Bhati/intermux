# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for InterMux Windows — standalone unprivileged InterMux.exe

IMPORTANT:
- InterMux.exe MUST run unprivileged (uac_admin=False).
- Elevated networking operations live exclusively in intermux-net.exe + wintun.dll
  installed into the admin-only %ProgramFiles%\InterMux\ directory.
"""

import os
import sys

if sys.platform != "win32":
    raise SystemExit("ERROR: windows/intermux_windows.spec must be run on Windows.")

_SPEC_DIR = os.path.dirname(os.path.abspath(SPEC))
_ROOT = os.path.dirname(_SPEC_DIR)

block_cipher = None

a = Analysis(
    [os.path.join(_SPEC_DIR, "gui", "app.py")],
    pathex=[_ROOT, _SPEC_DIR],
    binaries=[],
    datas=[],
    hiddenimports=[
        "tkinter",
        "tkinter.ttk",
        "tkinter.messagebox",
        "tkinter.filedialog",
        "intermux",
        "intermux.__main__",
        "intermux.cli",
        "intermux.config",
        "intermux.dnsconf",
        "intermux.errors",
        "intermux.state",
        "intermux.bugreport",
        "intermux.report",
        "intermux.helper",
        "intermux.helper.client",
        "intermux.helper.protocol",
        "intermux.apps",
        "intermux.platform",
        "intermux.platform.base",
        "intermux.platform.paths",
        "intermux.platform.windows",
        "intermux.platform.win_apps",
        "intermux.platform.win_deps",
        "intermux.platform.win_netinfo",
        "intermux.platform.win_pipe",
        "intermux.platform.win_vpn",
        "intermux.system",
        "intermux.system.shaping",
        "intermux.system.traffic",
        "intermux.ui",
        "intermux.ui.app",
        "intermux.ui.appicon",
        "intermux.ui.dialogs",
        "intermux.ui.graph",
        "intermux.ui.history",
        "intermux.ui.icons",
        "intermux.ui.monitor",
        "intermux.ui.picker",
        "intermux.ui.report_dialog",
        "intermux.ui.theme",
        "intermux.ui.tooltip",
        "intermux.ui.vpn",
        "intermux.ui.worker",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "numpy", "pandas", "matplotlib", "scipy", "PIL", "cv2", "sklearn",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="InterMux",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
    # CRITICAL SECURITY BOUNDARY: GUI and launched apps MUST run unprivileged.
    # Only intermux-net.exe elevates from admin-only %ProgramFiles%\InterMux\.
    uac_admin=False,
    version_file=None,
)
