# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build definition for Pixel Sweeper.

Build (from the project root, with the venv active):

    pyinstaller --clean --noconfirm PixelSweeper.spec

The result is a single windowed executable in ``dist/PixelSweeper.exe``, carrying
the icon from ``assets/pixelsweeper.ico`` (rebuild that with
``python assets/make_icon.py`` if the artwork changes).
"""

import os

ENTRY = os.path.join(SPECPATH, "main.py")
# Ships inside the bundle too, so the window icon can be set when the app runs
# frozen; the EXE icon below is what Explorer and the taskbar use for the file.
ICON = os.path.join(SPECPATH, "assets", "pixelsweeper.ico")

# PyQt6 ships far more than a widgets-only app needs. Dropping the unused
# bindings keeps the executable to roughly half the size, and none of them are
# reachable from this program.
EXCLUDES = [
    "tkinter",
    "PyQt6.Qt3DAnimation",
    "PyQt6.Qt3DCore",
    "PyQt6.Qt3DExtras",
    "PyQt6.Qt3DInput",
    "PyQt6.Qt3DLogic",
    "PyQt6.Qt3DRender",
    "PyQt6.QtBluetooth",
    "PyQt6.QtCharts",
    "PyQt6.QtDataVisualization",
    "PyQt6.QtDesigner",
    "PyQt6.QtHelp",
    "PyQt6.QtLocation",
    "PyQt6.QtMultimedia",
    "PyQt6.QtMultimediaWidgets",
    "PyQt6.QtNfc",
    "PyQt6.QtPdf",
    "PyQt6.QtPdfWidgets",
    "PyQt6.QtPositioning",
    "PyQt6.QtQml",
    "PyQt6.QtQuick",
    "PyQt6.QtQuick3D",
    "PyQt6.QtQuickWidgets",
    "PyQt6.QtRemoteObjects",
    "PyQt6.QtScxml",
    "PyQt6.QtSensors",
    "PyQt6.QtSerialPort",
    "PyQt6.QtSpatialAudio",
    "PyQt6.QtSql",
    "PyQt6.QtTest",
    "PyQt6.QtTextToSpeech",
    "PyQt6.QtWebChannel",
    "PyQt6.QtWebEngineCore",
    "PyQt6.QtWebEngineQuick",
    "PyQt6.QtWebEngineWidgets",
    "PyQt6.QtWebSockets",
]

a = Analysis(
    [ENTRY],
    pathex=[SPECPATH],
    binaries=[],
    datas=[(ICON, "assets")] if os.path.isfile(ICON) else [],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="PixelSweeper",
    icon=ICON if os.path.isfile(ICON) else None,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # GUI app: no console window
    disable_windowed_traceback=False,  # still shows a dialog on a hard crash
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
