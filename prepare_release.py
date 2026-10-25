"""Prepare the Windows download and matching library sources; never publish."""

from concurrent.futures import ThreadPoolExecutor
import argparse
import hashlib
from importlib import metadata
import json
from pathlib import Path, PurePosixPath
import posixpath
import shutil
import sys
import tarfile
import tomllib
from urllib.request import Request, urlopen
import zipfile


QT_VERSION = "6.12.0"
SOURCE_ARCHIVES = (
    ("qtbase-everywhere-src-6.12.0.tar.xz", "a951bd163c7b80fc6b8c88d7668fb56abf91c152373e13c10666763238131307"),
    ("qtsvg-everywhere-src-6.12.0.tar.xz", "e4ab39534ec97987b1b9b60ef7f4d3253d5912a69f8c713a01753174a4029331"),
    ("qtimageformats-everywhere-src-6.12.0.tar.xz", "0943132b5db8de6b6a7f1d162682173e8763c7ed94e80fd39113292845f67762"),
    ("pyside-setup-everywhere-src-6.12.0.tar.xz", "099c1a597cf33c1b000e11cf68a0b82d216fb802bf81726efc720394f10c7751"),
)


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def source_url(filename):
    if filename.startswith("pyside-"):
        return f"https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-{QT_VERSION}-src/{filename}"
    return f"https://download.qt.io/official_releases/qt/6.12/{QT_VERSION}/submodules/{filename}"


def get_sources(root):
    folder = root / "release" / "third-party-sources"
    folder.mkdir(parents=True, exist_ok=True)

    def get(record):
        filename, expected = record
        path = folder / filename
        if not path.is_file() or sha256(path) != expected:
            request = Request(source_url(filename), headers={"User-Agent": "Frame-Connect-release-preparation"})
            partial = path.with_suffix(path.suffix + ".part")
            with urlopen(request, timeout=45) as response, partial.open("wb") as output:
                shutil.copyfileobj(response, output)
            if sha256(partial) != expected:
                raise RuntimeError(f"Source checksum mismatch: {filename}")
            partial.replace(path)
        return {"filename": filename, "url": source_url(filename), "sha256": expected, "bytes": path.stat().st_size}

    with ThreadPoolExecutor(max_workers=4) as pool:
        return list(pool.map(get, SOURCE_ARCHIVES))


def copy_qt_notices(source, target):
    """Read selected members only; never extract archive paths onto disk."""
    records = []
    with tarfile.open(source) as archive:
        members = {member.name: member for member in archive if member.isfile()}
        prefix = next(iter(members)).split("/")[0]
        selected = {name for name in members if name.startswith(prefix + "/LICENSES/")}
        for name in members:
            if not name.endswith("qt_attribution.json"):
                continue
            content = archive.extractfile(members[name]).read().decode("utf-8")
            # Upstream attribution records can contain literal newlines in strings.
            data = json.loads(content, strict=False)
            for record in data if isinstance(data, list) else [data]:
                if "libs" not in record.get("QtParts", ["libs"]):
                    continue
                if record.get("QDocModule") not in ("qtcore", "qtgui", "qtnetwork", "qtwidgets", "qtimageformats", "QtForPython"):
                    continue
                records.append(dict(record, Source=name))
                directory = posixpath.dirname(name)
                files = record.get("LicenseFile", [])
                if isinstance(files, str):
                    files = [files]
                for filename in files:
                    resolved = posixpath.normpath(posixpath.join(directory, filename))
                    if not resolved.startswith(prefix + "/") or resolved not in members:
                        raise RuntimeError(f"Missing upstream license: {resolved}")
                    selected.add(resolved)
                for candidate in members:
                    basename = PurePosixPath(candidate).name.lower()
                    if candidate.startswith(directory + "/") and (
                        basename.startswith(("license", "licence", "copying", "copyright", "notice"))
                        or basename == "readme.ijg"
                    ):
                        selected.add(candidate)
        for name in sorted(selected):
            relative = PurePosixPath(name).relative_to(prefix)
            if ".." in relative.parts:
                raise RuntimeError("Unsafe source member")
            destination = target.joinpath(*relative.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(archive.extractfile(members[name]).read())
    return records


def prepare_licenses(root, sources):
    for package in ("PySide6-Essentials", "shiboken6"):
        if metadata.version(package) != QT_VERSION:
            raise RuntimeError(f"Update source pins/notices for the installed {package} version before releasing.")
    if sys.version_info[:3] != (3, 15, 0):
        raise RuntimeError("Review the Python runtime notices before releasing a different Python version.")
    licenses = root / "licenses"
    licenses.mkdir(exist_ok=True)
    shutil.copyfile(Path(sys.base_prefix) / "LICENSE.txt", licenses / "Python-3.15.0.txt")
    dist = metadata.distribution("pyinstaller")
    copying = next(path for path in dist.files if str(path).endswith("licenses/COPYING.txt"))
    shutil.copyfile(dist.locate_file(copying), licenses / "PyInstaller.txt")
    records = []
    for source in sources:
        filename = source["filename"]
        component = filename.split("-everywhere-src-")[0]
        records.extend(copy_qt_notices(root / "release" / "third-party-sources" / filename, licenses / component))
    (licenses / "Qt-attributions.json").write_text(json.dumps(records, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (licenses / "source-manifest.json").write_text(json.dumps(sources, indent=2) + "\n", encoding="utf-8")
    header = (
        "FRAME CONNECT — THIRD-PARTY NOTICES\n\n"
        "Frame Connect's own code is MIT licensed; see LICENSE.\n"
        "This distribution includes Python 3.15.0, Qt/PySide6/Shiboken 6.12.0,\n"
        f"and the PyInstaller {dist.version} bootloader/runtime hooks.\n"
        "Qt and PySide6: Copyright The Qt Company Ltd. and other contributors.\n"
        "The community Qt/PySide6/Shiboken libraries are used under LGPLv3.\n"
        "Their GPLv3 and LGPLv3 license texts are reproduced below.\n"
        "PyInstaller's bootloader exception permits application distribution;\n"
        "its runtime hooks are Apache-2.0 licensed.\n\n"
        "The read-only OpenVR binding references Valve's BSD-3-Clause API;\n"
        "see Valve-OpenVR.txt below. SteamVR's runtime and standalone controller\n"
        "icon files are read from the user's headset, not bundled with the app.\n\n"
        "The corresponding Qt/PySide6 sources are supplied in the companion\n"
        "ThirdPartySources download on the same release. See SOURCES.md for\n"
        "versions, checksums, rebuilding and replacing the libraries.\n"
        "Library modification and reverse engineering to debug modifications\n"
        "are not restricted by Frame Connect.\n\n"
        "The notices conservatively include optional upstream library components;\n"
        "some platform-specific components are not used in the Windows build.\n"
        "These components' licenses do not replace Frame Connect's MIT license.\n\n"
    )
    for record in records:
        copyright_text = record.get("Copyright", "")
        if isinstance(copyright_text, list):
            copyright_text = "\n".join(copyright_text)
        header += f"{record.get('Name', record['Id'])}\n{record.get('License', '')}\n{copyright_text}\n\n"
    # These notices cover the prebuilt opengl32sw.dll shipped in Qt's Windows wheel.
    for name in ("Qt-Mesa.txt", "Qt-LLVM.txt"):
        if not (licenses / name).is_file():
            raise RuntimeError(f"Required Windows Qt notice is missing: {name}")
    sections = [header]
    for path in sorted(licenses.rglob("*")):
        if path.is_file() and path.suffix.lower() != ".json":
            sections.append(f"\n{'=' * 72}\n{path.relative_to(licenses).as_posix()}\n{'=' * 72}\n\n")
            sections.append(path.read_text(encoding="utf-8", errors="replace"))
    (root / "THIRD_PARTY_NOTICES.txt").write_text("".join(sections), encoding="utf-8")


def package_release(root, sources):
    version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    output = root / "release"
    windows = output / f"FrameConnect-v{version}-Windows-x64.zip"
    source_zip = output / f"FrameConnect-v{version}-ThirdPartySources.zip"
    documents = (
        "LICENSE", "README.md", "THIRD_PARTY_NOTICES.txt", "SOURCES.md",
        "docs/assets/frame-connect.png", "docs/assets/frame-connect-home.png",
    )
    with zipfile.ZipFile(windows, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(root / "FrameConnect.exe", "FrameConnect.exe")
        for name in documents:
            archive.write(root / name, name)
        for path in sorted((root / "licenses").rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(root).as_posix())
    with zipfile.ZipFile(source_zip, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.write(root / "SOURCES.md", "SOURCES.md")
        archive.write(root / "licenses" / "source-manifest.json", "source-manifest.json")
        for record in sources:
            archive.write(output / "third-party-sources" / record["filename"], record["filename"])
    sums = output / "SHA256SUMS.txt"
    sums.write_text("".join(f"{sha256(path)}  {path.name}\n" for path in (windows, source_zip)), encoding="utf-8")
    return windows, source_zip, sums


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--licenses-only", action="store_true", help="Prepare notices/sources before building the executable")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    sources = get_sources(root)
    prepare_licenses(root, sources)
    if not args.licenses_only:
        for path in package_release(root, sources):
            print(path)


if __name__ == "__main__":
    main()
