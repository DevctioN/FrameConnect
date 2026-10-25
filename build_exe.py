"""Build a standalone Windows app with the theme and headset helpers included."""

import ctypes
import os
from pathlib import Path
import sys

import PyInstaller.__main__


def refresh_windows_icons() -> None:
    """Tell Explorer to discard icons cached for a previous executable build."""
    notify = ctypes.WinDLL("shell32").SHChangeNotify
    notify.argtypes = [ctypes.c_long, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
    notify.restype = None
    # SHCNE_ASSOCCHANGED, SHCNF_IDLIST | SHCNF_FLUSH; no file associations change.
    notify(0x08000000, 0x1000, None, None)


def main() -> None:
    if sys.platform != "win32":
        raise SystemExit("Build the Windows executable on Windows.")

    root = Path(__file__).resolve().parent
    os.environ.setdefault("PYINSTALLER_CONFIG_DIR", str(root / "build" / "cache"))
    icon = root / "ICO" / "icon.ico"
    if not icon.is_file():
        from PySide6.QtGui import QImage

        icon = root / "build" / "icon.ico"
        icon.parent.mkdir(parents=True, exist_ok=True)
        image = QImage(str(root / "docs" / "assets" / "frame-connect.png"))
        if image.isNull() or not image.save(str(icon), "ICO"):
            raise SystemExit("Could not create an icon from the README logo.")
    options = [
        str(root / "main.py"),
        "--name", "FrameConnect",
        "--onefile",
        "--windowed",
        "--noconfirm",
        "--noupx",
        "--icon", str(icon),
        "--add-data", f"{icon}:ICO",
        "--distpath", str(root),
        "--workpath", str(root / "build" / "pyinstaller"),
        "--specpath", str(root / "build"),
    ]
    for filename in ("styles.qss", "probe.py", "vr_status.py", "remote_files.py", "steam_api.py", "remote_power.py"):
        options.extend(("--add-data", f"{root / filename}:."))
    for filename in ("LICENSE", "THIRD_PARTY_NOTICES.txt", "SOURCES.md", "licenses"):
        if not (root / filename).exists():
            raise SystemExit("Run prepare_release.py --licenses-only to prepare the license files before building.")
        options.extend(("--add-data", f"{root / filename}:{filename if filename == 'licenses' else '.'}"))
    PyInstaller.__main__.run(options)
    refresh_windows_icons()


if __name__ == "__main__":
    main()
