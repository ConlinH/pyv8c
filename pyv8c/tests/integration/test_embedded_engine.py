"""The browser and public V8 API must use one native module identity."""

import asyncio
import subprocess
import sys
import ast
from pathlib import Path


async def test_public_engine_and_browser_share_native_module():
    import pyv8c
    from pyv8c import pyv8
    import pyv8c.pyv8 as engine
    from pyv8c.engine import binder

    assert engine is pyv8 is binder.pyv8
    assert engine.__version__ == pyv8c.__version__ == '0.1.0'
    assert engine.IdlSpec is binder.IdlSpec
    assert engine.Context.__module__ == 'pyv8c.pyv8'
    assert engine.IdlSpec.__module__ == 'pyv8c.pyv8'


async def test_source_does_not_import_the_top_level_engine():
    package = Path(__file__).resolve().parents[2] / 'pyv8c'
    for path in package.rglob('*.py'):
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(alias.name != 'pyv8' and not alias.name.startswith('pyv8.') for alias in node.names), path
            if isinstance(node, ast.ImportFrom) and not node.level:
                assert node.module != 'pyv8' and not (node.module or '').startswith('pyv8.'), path


async def test_trace_does_not_import_the_top_level_pyv8_package():
    source = '''
import sys
from pyv8c import pyv8
class Window: pass
context = pyv8.Context(Window(), trace=True)
try:
    assert context.exec_js('40 + 2') == 42
finally:
    context.close()
assert 'pyv8' not in sys.modules
assert 'pyv8c.pyv8.trace' in sys.modules
'''
    result = (await asyncio.to_thread(subprocess.run, [sys.executable, '-c', source], capture_output=True, text=True, timeout=30))
    assert result.returncode == 0, result.stdout + result.stderr
