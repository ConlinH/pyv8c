"""Verify that the open-source license metadata and bundled notices stay in sync.

Run locally with ``python tools/check_license.py`` or from the
``license-check`` GitHub Actions workflow.  The check is intentionally
filesystem-only: it must not require the V8 toolchain or a built extension.
It also pins the location of the community governance files under ``.github/``.
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import tarfile
import tomllib
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
LICENSE_FILES = (
    "LICENSE", "THIRD_PARTY_NOTICES.md",
    "third_party_licenses/V8-LICENSE.txt", "third_party_licenses/ICU-LICENSE.txt",
    "third_party_licenses/CHROMIUM-LICENSE.txt",
    "third_party_licenses/BLINK-AUDIO-LICENSE.txt",
    "third_party_licenses/STRONGTALK-LICENSE.txt",
    "third_party_licenses/AUDIO-RUST-CRATES-LICENSES.txt",
    "third_party_licenses/SKIA-LICENSE.txt",
    "third_party_licenses/RUST-SKIA-LICENSE.txt",
    "third_party_licenses/SKIA-THIRD-PARTY-LICENSES.txt",
    "third_party_licenses/LAYOUT-RUST-CRATES-LICENSES.txt",
)
GOVERNANCE_FILES = (
    ".github/CONTRIBUTING.md",
    ".github/CODE_OF_CONDUCT.md",
    ".github/SECURITY.md",
)
LICENSE_PHRASES = (
    "MIT License",
    "Permission is hereby granted, free of charge",
    'THE SOFTWARE IS PROVIDED "AS IS"',
)
NOTICE_PHRASES = ("V8", "Chromium", "BSD-3-Clause", "icudtl.dat", "Temporal")


def check_artifact(path: pathlib.Path, errors: list[str]) -> None:
    """Check archived bytes, not just the source tree or metadata declarations."""
    try:
        if path.suffix == ".whl":
            with zipfile.ZipFile(path) as archive:
                members = {n: archive.read(n) for n in archive.namelist()
                           if ".dist-info/licenses/" in n}
            expected = {n.split(".dist-info/licenses/", 1)[1]: data
                        for n, data in members.items()}
        else:
            with tarfile.open(path, "r:gz") as archive:
                expected = {m.name.split("/", 1)[1]: archive.extractfile(m).read()
                            for m in archive.getmembers()
                            if m.isfile() and "/" in m.name}
        for name in LICENSE_FILES:
            data = expected.get(name)
            if data is None:
                errors.append(f"{path}: missing {name}")
            elif data.replace(b"\r\n", b"\n") != (ROOT / name).read_bytes().replace(b"\r\n", b"\n"):
                errors.append(f"{path}: {name} differs from source")
    except (OSError, tarfile.TarError, zipfile.BadZipFile, KeyError) as error:
        errors.append(f"cannot inspect {path}: {error}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=pathlib.Path, action="append", default=[],
                        help="also inspect a wheel or .tar.gz sdist; repeatable")
    args = parser.parse_args()
    errors: list[str] = []
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = data["project"]

    if "license" in project:
        errors.append("distribution license expression requires a completed linked-dependency audit")

    matched: set[str] = set()
    for pattern in project.get("license-files", []):
        for hit in ROOT.glob(pattern):
            if hit.is_file():
                matched.add(hit.relative_to(ROOT).as_posix())

    for name in LICENSE_FILES:
        if name not in matched:
            errors.append(f"{name} is not matched by project.license-files")
        path = ROOT / name
        if not path.is_file() or not path.read_text(encoding="utf-8").strip():
            errors.append(f"{name} is missing or empty")

    if (ROOT / "LICENSE").is_file():
        text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        for phrase in LICENSE_PHRASES:
            if phrase not in text:
                errors.append(f"LICENSE is missing or altered: {phrase}")

    if (ROOT / "THIRD_PARTY_NOTICES.md").is_file():
        notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
        for phrase in NOTICE_PHRASES:
            if phrase not in notices:
                errors.append(f"THIRD_PARTY_NOTICES.md is missing: {phrase}")

    for name in GOVERNANCE_FILES:
        path = ROOT / name
        if not path.is_file() or not path.read_text(encoding="utf-8").strip():
            errors.append(f"{name} is missing or empty")

    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    for name in LICENSE_FILES[:2]:
        if f"include {name}" not in manifest:
            errors.append(f"MANIFEST.in must include {name} so sdist carries it")
    if "recursive-include third_party_licenses *.txt" not in manifest:
        errors.append("MANIFEST.in must include third_party_licenses/*.txt")

    for artifact in args.artifact:
        check_artifact(artifact, errors)

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for target in ("LICENSE", "THIRD_PARTY_NOTICES.md", ".github/SECURITY.md"):
        if f"]({target})" not in readme:
            errors.append(f"README.md should link to {target}")

    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1

    print("License file checks passed; linked third-party dependencies still require a release audit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
