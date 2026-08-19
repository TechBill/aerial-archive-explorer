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

executable = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="Aerial Archive Explorer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=str(project_root / "assets" / icon_name),
)

if sys.platform == "darwin":
    application = BUNDLE(
        executable,
        name="Aerial Archive Explorer.app",
        icon=str(project_root / "assets" / "icon.icns"),
        bundle_identifier="com.techbill.aerialarchiveexplorer",
        info_plist={
            "CFBundleDisplayName": "Aerial Archive Explorer",
            "NSHighResolutionCapable": True,
        },
    )
