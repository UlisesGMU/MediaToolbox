#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Builds Media Toolbox into a standalone program with PyInstaller, so it runs
on machines without Python.

    python build_exe.py            one file:   dist/MediaToolbox(.exe)
    python build_exe.py --onedir   one folder: dist/MediaToolbox/ (starts faster)

Optional packages (pymediainfo, mutagen, xxhash, send2trash) are included
when they're installed on this machine, and the matching tabs/options work
in the built program; missing ones are listed before building.

The version comes from APP_VERSION in common.py; on Windows it's also
written into the .exe (right-click > Properties > Details).

The result only runs on the same OS and architecture (e.g. 64-bit Windows)
as the Python that built it.
"""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
NAME = "MediaToolbox"

# (package, what it enables, PyInstaller options that make sure it's bundled completely)
OPTIONAL = [
    # --collect-all also copies the MediaInfo library (DLL/.so) that pymediainfo ships with.
    ("pymediainfo", "Video Check, and movies by length in Video Sorter", ["--collect-all", "pymediainfo"]),
    # mutagen loads each audio format's module on demand; bundle them all.
    ("mutagen", "Music Sorter", ["--collect-submodules", "mutagen"]),
    ("xxhash", "the xxHash checksum", []),
    ("send2trash", "deleting to the Recycle Bin instead of permanently", ["--collect-submodules", "send2trash"]),
]


def app_version() -> str:
    """Read APP_VERSION from common.py as text, without importing it (no Qt needed)."""
    m = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', (HERE / "common.py").read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else "0.0.0"


def windows_version_file(version: str) -> Path:
    """The version resource PyInstaller embeds in the .exe (shown in its Properties)."""
    nums = [int(x) for x in re.findall(r"\d+", version)][:4]
    nums += [0] * (4 - len(nums))
    path = HERE / "build" / "version_info.txt"
    path.parent.mkdir(exist_ok=True)
    path.write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={tuple(nums)}, prodvers={tuple(nums)}, mask=0x3f, flags=0x0,
                    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('ProductName', 'Media Toolbox'),
      StringStruct('FileDescription', 'Media Toolbox'),
      StringStruct('FileVersion', '{version}'),
      StringStruct('ProductVersion', '{version}'),
      StringStruct('OriginalFilename', '{NAME}.exe'),
      StringStruct('LegalCopyright', 'GPL-3.0')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""", encoding="utf-8")
    return path


def installed(package: str) -> bool:
    return importlib.util.find_spec(package) is not None


def main() -> int:
    if not installed("PyInstaller"):
        print("PyInstaller isn't installed. Install it with:\n    pip install pyinstaller")
        return 1
    if not installed("PySide6"):
        print("PySide6 isn't installed. Install it with:\n    pip install PySide6")
        return 1

    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--name", NAME,
        "--windowed",                 # No console window behind the app.
        "--onedir" if "--onedir" in sys.argv else "--onefile",
        "--paths", str(HERE),         # So the tab modules next to media_toolbox.py are found.
    ]
    version = app_version()
    print(f"Media Toolbox {version}\n")
    if sys.platform.startswith("win"):
        args += ["--version-file", str(windows_version_file(version))]
    print("Optional packages:")
    for package, enables, extra in OPTIONAL:
        if installed(package):
            print(f"  [included] {package:12} {enables}")
            args += extra
        else:
            print(f"  [missing]  {package:12} {enables}  (pip install {package})")
    args.append(str(HERE / "media_toolbox.py"))

    print("\nBuilding… (this takes a minute or two)\n")
    result = subprocess.run(args, cwd=HERE)
    if result.returncode != 0:
        print("\nThe build failed; the messages above say why.")
        return result.returncode

    out = HERE / "dist" / (NAME if "--onedir" in sys.argv else NAME + (".exe" if sys.platform.startswith("win") else ""))
    print(f"\nDone: {out}  (version {version})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
