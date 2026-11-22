"""Run audio and DOM regressions against a wheel installed outside the checkout."""

from pathlib import Path
import pyv8c
import pytest


root = Path(__file__).resolve().parents[1]
package = Path(pyv8c.__file__).resolve()
if package.is_relative_to(root):
    raise SystemExit(f'Expected the installed wheel, found checkout package: {package}')

tests = root / 'pyv8c' / 'tests'
print(f'Native component tests against installed package: {package}', flush=True)
raise SystemExit(pytest.main([
    '-q', '-o', 'pythonpath=', '-o', 'addopts=',
    str(tests / 'unit' / 'test_audio_compressor_precision.py'),
    str(tests / 'unit' / 'test_audio_native.py'),
    str(tests / 'browser' / 'test_dom_native.py'),
    str(tests / 'integration' / 'test_audio_rendering.py')
    + '::test_offline_triangle_compressor_matches_chrome_float32_sum',
    str(tests / 'integration' / 'test_audio_rendering.py')
    + '::test_compressor_automation_clamps_after_interpolation_at_render_quanta',
    str(tests / 'integration' / 'test_audio_rendering.py')
    + '::test_compressor_parameter_value_and_automation_rate_match_chrome',
]))
