"""IDL versions do not carry a second Window installation order."""
import json
import zipfile
from pathlib import Path

from pyv8c.engine import idl as idl_module
from pyv8c.engine.idl import DEFAULT_IDL_PATH, IDL_VERSION, Idl, idl_path


def test_versioned_baselines_use_current_schema_without_window_order():
    root = Path(__file__).resolve().parents[2] / 'idls'
    for version in ('145.0.7632.162', '154.0.8030.0', '158.0.8127.36'):
        path = root / f'{version}.zip'
        with zipfile.ZipFile(path) as archive:
            document = json.loads(archive.read(archive.namelist()[0]))
        assert document['idl']['version'] == IDL_VERSION
        assert document['idl']['chromium_version'] == version
        assert 'window' not in document
        assert Idl.load(path, profile=None).metadata['chromium_version'] == version


def test_idl_path_follows_the_host():
    assert idl_path('win32') == DEFAULT_IDL_PATH
    assert idl_path('darwin') == DEFAULT_IDL_PATH.with_name('idl-158.0.8127.36.zip')
    assert idl_path('darwin').is_file()
    assert idl_path('linux') == DEFAULT_IDL_PATH
    assert Idl.load().path == 'pyv8c:idl/idl.zip'
    assert Idl.load(idl_path('darwin')).path == 'pyv8c:idl/idl-158.0.8127.36.zip'


def test_idl_path_falls_back_when_the_host_file_is_missing(monkeypatch, tmp_path):
    missing = tmp_path / 'idl-missing.zip'
    monkeypatch.setattr(idl_module, '_HOST_IDLS', {'darwin': missing, 'win32': DEFAULT_IDL_PATH})
    assert idl_path('darwin') == DEFAULT_IDL_PATH
