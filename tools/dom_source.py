"""Identify the DOM sources that Cargo links into the native extension."""

import hashlib
from pathlib import Path


def dom_source_sha256(source_dir: Path) -> str:
    """Match the fingerprint emitted by native/build.rs."""
    digest = hashlib.sha256()
    for path in sorted(source_dir.iterdir(), key=lambda path: path.name):
        if path.suffix in (".cpp", ".h"):
            digest.update(path.name.encode("utf-8"))
            digest.update(b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()
