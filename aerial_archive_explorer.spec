# PyInstaller build definition for Aerial Archive Explorer.

import sys
from pathlib import Path

project_root = Path(SPECPATH)
icon_name = "icon.ico" if sys.platform == "win32" else "icon.icns"

analysis = Analysis(
    [str(project_root / "aerial_archive_explorer.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=[(str(project_root / "assets" / icon_name), "assets")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(analysis.pure)

# Build onedir (exclude_binaries + COLLECT) rather than onefile. A onefile
# build's bootloader has to self-extract to a temp directory on every
# launch before the real process starts, which is what causes the Dock/
# taskbar icon to appear, vanish, and then reappear after a delay. Onedir
# runs the real executable directly from the unpacked bundle, so the icon
# stays put continuously from launch through window appearance.
executable = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="Aerial Archive Explorer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=str(project_root / "assets" / icon_name),
)

collection = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=True,
    name="Aerial Archive Explorer",
)

if sys.platform == "darwin":
    application = BUNDLE(
        collection,
        name="Aerial Archive Explorer.app",
        icon=str(project_root / "assets" / "icon.icns"),
        bundle_identifier="com.techbill.aerialarchiveexplorer",
        info_plist={
            "CFBundleDisplayName": "Aerial Archive Explorer",
            "CFBundleShortVersionString": "2.0.1",
            "CFBundleVersion": "2.0.1",
            "NSHighResolutionCapable": True,
        },
    )
