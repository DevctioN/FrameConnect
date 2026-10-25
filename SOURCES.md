# Library sources and replacement

Frame Connect's own code is licensed under MIT; see [LICENSE](LICENSE).
Python, Qt, PySide6, Shiboken and other bundled components retain their own
licenses. Their notices and complete license texts are in
[THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt) and `licenses/`.

The read-only controller helper references Valve's public OpenVR API; its
attribution is in `licenses/Valve-OpenVR.txt`. SteamVR's runtime library and
standalone controller icon files are loaded from the user's headset, rather
than bundled with Frame Connect.

## Corresponding library source

The Windows release uses Python 3.15.0 and the unmodified community
PySide6-Essentials/Shiboken 6.12.0 wheels, including Qt 6.12.0. Frame Connect
does not modify those libraries. The companion
`FrameConnect-v0.1.8-ThirdPartySources.zip` release asset contains the complete
upstream source archives for the Qt modules shipped with this app:

- `qtbase-everywhere-src-6.12.0.tar.xz`: Qt Core, GUI, Widgets, Network and platform plugins.
- `qtsvg-everywhere-src-6.12.0.tar.xz`: SVG plugins/library.
- `qtimageformats-everywhere-src-6.12.0.tar.xz`: additional image-format plugins.
- `pyside-setup-everywhere-src-6.12.0.tar.xz`: PySide6 and Shiboken bindings.

These archives include upstream build scripts and third-party code. Official
download URLs, versions, byte sizes and SHA-256 hashes are recorded in
`licenses/source-manifest.json`. Publish the companion source asset alongside
the Windows download and retain it for recipients of that version. Its contents
are copies supplied by this release, rather than only links to another host.
Python's source is also available from
[Python 3.15.0](https://www.python.org/downloads/release/python-3150/).

## Use a modified library

Frame Connect permits library modification and reverse engineering for debugging
those modifications. You can run its Python source against modified Qt/PySide6
libraries, or rebuild the Windows executable using them. The app does not use
signatures, encryption or an activation check to prevent replacement.

1. Download Frame Connect's source for this release and install Python 3.15
   and `uv`. Run `uv sync --locked --group build` once to create the build environment.
2. Build compatible Qt/PySide6/Shiboken libraries from the companion archives.
   Use the included build instructions and the official
   [Qt Windows build guide](https://doc.qt.io/qt-6/windows-building.html) and
   [Qt for Python build guide](https://doc.qt.io/qtforpython-6/building_from_source/index.html).
   Building the libraries requires a compatible MSVC compiler, Windows SDK,
   CMake/Ninja and the other prerequisites described by those guides.
3. Install your compatible replacement wheels into `.venv`, for example:
   `uv pip install --python .venv/Scripts/python.exe --reinstall path/to/shiboken6.whl path/to/PySide6_Essentials.whl`.
   Use the actual wheel filenames. Alternatively replace compatible Qt DLLs
   and plugins in `.venv/Lib/site-packages/PySide6/` with your build's equivalents.
4. Run `.venv\Scripts\python.exe main.py` to test the app with your libraries.
   Do not run `uv sync` again afterward, since that restores the locked wheels.
5. Run `.venv\Scripts\python.exe build_exe.py` to freeze that environment into
   a new executable. If `ICO/icon.ico` is absent, the build script converts
   the public README logo into a build icon. Rebuilding does not need the
   maintainer's private files or email address.

`prepare_release.py` pins this release's original library versions and source
hashes. When distributing a modified library build, update its source manifest
and notices to match your modifications. The normal Windows ZIP contains the
EXE and notices; keep those files together when redistributing it.
