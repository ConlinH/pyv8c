"""Data-only platform modules must not initialize the browser runtime."""
import os
from pathlib import Path
import subprocess
import sys


def test_resource_records_and_css_sizes_do_not_import_native_runtime():
    script = '''
import importlib.abc
import sys
class BlockNative(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'pyv8c.pyv8' or fullname.startswith('pyv8c.pyv8.'):
            raise AssertionError('unexpected native import: ' + fullname)
sys.meta_path.insert(0, BlockNative())
import pyv8c.platform as platform
from pyv8c.platform.resources.tracker import DocumentResourceTracker
from pyv8c.platform.resources.ownership import ResourceOwnership
from pyv8c.foundation.lifecycle import ResourceScope
from pyv8c.foundation.font_sizes import font_size_px
assert not DocumentResourceTracker().has_pending
assert ResourceOwnership(ResourceScope()).active_count == 0
assert font_size_px('2em', 24) == 48
assert 'pyv8c.platform.browser' not in sys.modules
assert 'pyv8c.pyv8' not in sys.modules
try:
    platform.missing_capability
except AttributeError:
    pass
else:
    raise AssertionError('unknown platform export was accepted')
'''
    repo = Path(__file__).resolve().parents[2]
    result = subprocess.run([sys.executable, '-c', script], cwd=repo,
                            env=dict(os.environ, PYTHONPATH=str(repo)),
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
