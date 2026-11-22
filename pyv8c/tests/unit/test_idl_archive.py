"""The shipped document is an archive: it expands beside itself and caches what it decoded.

The digest covers the JSON payload, so an archive and the sidecar it expands to are the same
document to everything downstream, and any cache trouble rebuilds instead of reporting.
"""
import os
import pytest
import zipfile
from pathlib import Path

from pyv8c.engine import idl as module
from pyv8c.engine.idl import Idl, write_idl


def _document(version=None):
    return {"idl": {"version": module.IDL_VERSION if version is None else version},
            "interfaces": {"Node": {}, "Element": {"parent": "Node"}}}


@pytest.mark.parametrize("version", [8, 9, 11])
async def test_unsupported_format_versions_are_rejected(version):
    with pytest.raises(ValueError, match="unsupported IDL version"):
        module._read_document(_document(version))


def _forget():
    """Drop the in-process memories, so the next read is a real read."""
    module._DOCUMENTS.clear()
    module._VIEWS.clear()


def _member(archive):
    with zipfile.ZipFile(archive) as opened:
        return opened.read("idl.json")


async def test_a_zip_path_writes_one_member_named_after_the_archive(tmp_path):
    archive = write_idl(tmp_path / "idl.zip", _document())
    plain = write_idl(tmp_path / "idl.json", _document())

    assert archive.name == "idl.zip"
    with zipfile.ZipFile(archive) as opened:
        assert opened.namelist() == ["idl.json"]
    assert Idl.load(archive, profile=None).digest == Idl.load(plain, profile=None).digest


async def test_the_archive_is_the_smaller_spelling_of_one_document(tmp_path):
    """Why a release ships the archive: same bytes inside, a fraction of the size outside."""
    document = {"idl": {"version": module.IDL_VERSION},
                "interfaces": {f"Interface{index}": {"parent": "Node"} for index in range(400)}}
    archive = write_idl(tmp_path / "idl.zip", document)
    plain = write_idl(tmp_path / "idl.json", document)

    assert archive.stat().st_size < plain.stat().st_size / 4


async def test_the_archive_expands_beside_itself_and_caches_what_it_decoded(tmp_path):
    archive = write_idl(tmp_path / "idl.zip", _document())
    sidecar, cache = tmp_path / "idl.json", tmp_path / "idl.pyv8ccache"

    _forget()
    timings = {}
    view = Idl.load(archive, profile=None, timings=timings)

    assert sorted(view.interfaces) == ["Element", "Node"]
    assert sidecar.read_bytes() == _member(archive)
    assert cache.is_file()
    assert timings["idl_binary_cache"] is False


async def test_a_cache_hit_skips_the_parse_and_the_checks(tmp_path):
    document = tmp_path / "idl.json"
    write_idl(document, _document())
    Idl.load(document, profile=None)

    _forget()
    timings = {}
    Idl.load(document, profile=None, timings=timings)

    assert timings["idl_binary_cache"] is True


async def test_load_timings_are_owned_by_each_call(tmp_path):
    document = write_idl(tmp_path / "idl.json", _document())
    _forget()
    first, second = {}, {}
    Idl.load(document, profile=None, timings=first)
    Idl.load(document, profile=None, timings=second)
    assert first['idl_cached'] is False
    assert second['idl_cached'] is True


async def test_a_broken_cache_is_rebuilt_instead_of_reported(tmp_path):
    document = tmp_path / "idl.json"
    write_idl(document, _document())
    cache = tmp_path / "idl.pyv8ccache"
    cache.write_bytes(b"not a cache at all")

    _forget()
    timings = {}
    view = Idl.load(document, profile=None, timings=timings)

    assert sorted(view.interfaces) == ["Element", "Node"]
    assert timings["idl_binary_cache"] is False
    assert cache.read_bytes().startswith(module._CACHE_MAGIC)


async def test_a_rewritten_archive_wins_over_its_stale_sidecar(tmp_path):
    archive = write_idl(tmp_path / "idl.zip", _document())
    sidecar = tmp_path / "idl.json"
    sidecar.write_bytes(b'{"idl": {"version": 0}, "interfaces": {"Gone": {}}}')
    os.utime(sidecar, (0, 0))                       # older than the archive

    _forget()
    view = Idl.load(archive, profile=None)

    assert sorted(view.interfaces) == ["Element", "Node"]
    assert sidecar.read_bytes() == _member(archive)


async def test_an_archive_the_reader_cannot_trust_is_refused(tmp_path):
    two = tmp_path / "two.zip"
    with zipfile.ZipFile(two, "w") as opened:
        opened.writestr("idl.json", b"{}")
        opened.writestr("other.json", b"{}")
    unsafe = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(unsafe, "w") as opened:
        opened.writestr("../idl.json", b"{}")
    wrong = tmp_path / "idl.txt"
    wrong.write_bytes(b"{}")

    with pytest.raises(ValueError, match="exactly one JSON member"):
        Idl.load(two, profile=None)
    with pytest.raises(ValueError, match="unsafe member path"):
        Idl.load(unsafe, profile=None)
    with pytest.raises(ValueError, match="must be a .zip or a .json file"):
        Idl.load(wrong, profile=None)


async def test_the_document_this_build_reads_is_the_archive_it_ships():
    """The one artifact in the tree, and the name a Runtime reports for it."""
    assert Path(module.DEFAULT_IDL_PATH).name == "idl.zip"
    assert Path(module.DEFAULT_IDL_PATH).is_file()


async def test_failed_cache_replacement_preserves_the_published_cache(tmp_path, monkeypatch):
    path = tmp_path / 'idl.pyv8ccache'
    path.write_bytes(b'published')
    def fail(*args, **kwargs):
        raise OSError('cannot replace')
    monkeypatch.setattr(module.os, 'replace', fail)
    assert module._write_cache(path, 'digest', {}, {}) is False
    assert path.read_bytes() == b'published'
    assert sorted(p.name for p in tmp_path.iterdir()) == ['idl.pyv8ccache']


async def test_sidecar_remains_complete_while_its_replacement_is_written(tmp_path):
    path = tmp_path / 'idl.json'
    path.write_bytes(b'old-complete')
    def write(handle):
        handle.write(b'new-')
        handle.flush()
        assert path.read_bytes() == b'old-complete'
        handle.write(b'complete')
    module._replace_sidecar(path, write)
    assert path.read_bytes() == b'new-complete'
    assert len(list(tmp_path.iterdir())) == 1
