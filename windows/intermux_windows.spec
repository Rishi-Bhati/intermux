# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for InterMux Windows — standalone .exe

⚠ IMPORTANT: This spec MUST be run on Windows (or via GitHub Actions windows-latest).
   Running on Linux produces a Linux ELF binary, not a .exe.

Build (on a Windows machine):
  pip install pyinstaller psutil
  pyinstaller windows\\intermux_windows.spec

Or use the GitHub Actions workflow:
  .github/workflows/build_windows.yml
  → Push to main, go to Actions tab → download InterMux.exe artifact

Output: dist\\InterMux.exe  (single-file, no installer needed)
"""

import sys
import os

if sys.platform != "win32":
    raise SystemExit(
        "\n"
        "  ╔══════════════════════════════════════════════════════════════╗\n"
        "  ║  ERROR: This spec must be run on Windows, not Linux/macOS.  ║\n"
        "  ║                                                              ║\n"
        "  ║  PyInstaller cannot cross-compile. A Linux build produces   ║\n"
        "  ║  a Linux ELF binary — NOT a Windows .exe.                   ║\n"
        "  ║                                                              ║\n"
        "  ║  Options:                                                    ║\n"
        "  ║  1. Push to GitHub → Actions tab → download InterMux.exe    ║\n"
        "  ║  2. Run this on a Windows machine                           ║\n"
        "  ╚══════════════════════════════════════════════════════════════╝\n"
    )


# Path helpers
_SPEC_DIR  = os.path.dirname(os.path.abspath(SPEC))   # windows/
_ROOT      = os.path.dirname(_SPEC_DIR)                # repo root

block_cipher = None

a = Analysis(
    # Entry point — use the GUI by default
    [os.path.join(_SPEC_DIR, 'gui', 'app.py')],
    pathex=[_ROOT, _SPEC_DIR],
    binaries=[],
    datas=[],
    hiddenimports=[
        # psutil sub-modules needed on Windows
        'psutil',
        'psutil._pswindows',
        'psutil._common',
        # asyncio internals
        'asyncio',
        'asyncio.windows_events',
        'asyncio.windows_utils',
        # Tkinter
        'tkinter',
        'tkinter.ttk',
        'tkinter.messagebox',
        'tkinter.filedialog',
        # Our own modules
        'windows.core.interface',
        'windows.core.proxy_engine',
        'windows.core.app_launcher',
        'windows.core.platform_utils',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Exclude Linux-only modules to keep the exe small
        'core.interface',
        'core.router',
        'core.platform_utils',
        'gui.app',
        # Large unused packages
        'numpy', 'pandas', 'matplotlib', 'scipy',
        'PIL', 'cv2', 'sklearn',
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
    name='InterMux',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,             # Compress with UPX if available
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,        # Windowed mode (no console window on launch)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,            # Add a .ico file path here if you have one
    # Request UAC elevation on launch (recommended for socket binding)
    uac_admin=True,
    version_file=None,
)
