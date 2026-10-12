# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for InterMux Linux (onedir build).

Build (on a Linux machine):
  pip install pyinstaller
  pyinstaller intermux_linux.spec

Or use the GitHub Actions workflow:
  .github/workflows/build_linux.yml

Or use install.sh, which builds and installs in one step.

Output: dist/intermux/intermux (onedir bundle; the directory is installed to /usr/lib/intermux)
"""

import sys
import os

if sys.platform != "linux":
    raise SystemExit(
        "\n"
        "  ╔══════════════════════════════════════════════════════════════╗\n"
        "  ║  ERROR: This spec must be run on Linux, not Windows/macOS.   ║\n"
        "  ║                                                              ║\n"
        "  ║  PyInstaller cannot cross-compile. A Windows build produces  ║\n"
        "  ║  a Windows .exe — NOT a Linux ELF binary.                    ║\n"
        "  ║                                                              ║\n"
        "  ║  Options:                                                    ║\n"
        "  ║  1. Push to GitHub → Actions tab → download intermux         ║\n"
        "  ║  2. Run this on a Linux machine                              ║\n"
        "  ╚══════════════════════════════════════════════════════════════╝\n"
    )

# Path helpers
_ROOT = os.path.dirname(os.path.abspath(SPEC))

a = Analysis(
    # Entry point: `intermux` with no arguments opens the GUI; `intermux helper` runs the root helper
    [os.path.join(_ROOT, 'intermux', '__main__.py')],
    pathex=[_ROOT],
    binaries=[],
    datas=[],
    hiddenimports=[
        'tkinter',
        'tkinter.ttk',
        'tkinter.filedialog',
        'tkinter.messagebox',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Large unused packages
        'numpy', 'pandas', 'matplotlib', 'scipy',
        'PIL', 'cv2', 'sklearn',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='intermux',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,         # Keep console for pkexec/sudo prompts and CLI output
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='intermux',      # -> dist/intermux/
)
