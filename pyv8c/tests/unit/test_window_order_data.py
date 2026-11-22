"""IDL versions do not carry a second Window installation order."""
import json
import zipfile
from pathlib import Path

from pyv8c.engine.idl import IDL_VERSION, Idl


def test_versioned_baselines_use_current_schema_without_window_order():
    root = Path(__file__).resolve().parents[2] / 'idls'
    for version in ('145.0.7632.162', '154.0.8030.0'):
        path = root / f'{version}.zip'
        with zipfile.ZipFile(path) as archive:
            document = json.loads(archive.read(archive.namelist()[0]))
        assert document['idl']['version'] == IDL_VERSION
        assert document['idl']['chromium_version'] == version
        assert 'window' not in document
        assert Idl.load(path, profile=None).metadata['chromium_version'] == version
