import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


def _module():
    path = Path(__file__).resolve().parents[1] / 'tools' / 'validate_v8_cache.py'
    spec = importlib.util.spec_from_file_location('pyv8_cache_validation', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cache_must_include_link_inputs_and_compilers(tmp_path, monkeypatch):
    module = _module()
    home = tmp_path / 'v8'
    output = home / 'out'
    llvm = home / 'third_party/llvm-build/Release+Asserts/bin'
    llvm.mkdir(parents=True)
    (llvm / 'clang-cl.exe').touch()
    (llvm / 'lld-link.exe').touch()
    obj = output / 'obj'
    obj.mkdir(parents=True)
    (obj / 'mksnapshot.ninja').write_text('build ./mksnapshot.exe: link obj/temporal.rlib\n')
    archive = obj / 'temporal.rlib'
    archive.touch()
    config = SimpleNamespace(v8_home=home, output_dir=output, _can_reuse_build=lambda: True)
    monkeypatch.setattr(module, 'cppgc_microtask_queue_enabled', lambda path: True)
    module.validate_cache(config)
    archive.unlink()
    with pytest.raises(module.V8BuildError, match='link input was not found'):
        module.validate_cache(config)


def test_cache_requires_matching_build_stamp():
    module = _module()
    config = SimpleNamespace(_can_reuse_build=lambda: False)
    with pytest.raises(module.V8BuildError, match='stamp'):
        module.validate_cache(config)


@pytest.mark.parametrize("reusable", [True, False])
def test_restored_artifact_is_kept_only_when_valid(tmp_path, monkeypatch, reusable):
    module = _module()
    home = tmp_path / '.pyv8-build' / 'versions' / '1.2.3' / 'v8'
    home.mkdir(parents=True)
    (home / 'include').mkdir()
    config = SimpleNamespace(v8_home=home, version='1.2.3',
                             _can_reuse_build=lambda: reusable)
    if reusable:
        monkeypatch.setattr(module, 'validate_cache', lambda config: None)
    assert module.prepare_restored_cache(config, tmp_path) is reusable
    assert home.exists() is reusable


def test_cleanup_preserves_a_real_checkout_and_rejects_an_external_path(tmp_path):
    module = _module()
    checkout = tmp_path / '.pyv8-build' / 'versions' / '1.2.3' / 'v8'
    (checkout / '.git').mkdir(parents=True)
    config = SimpleNamespace(v8_home=checkout, version='1.2.3',
                             _can_reuse_build=lambda: False)
    with pytest.raises(module.V8BuildError, match='Refusing to discard'):
        module.prepare_restored_cache(config, tmp_path)
    assert (checkout / '.git').is_dir()

    config.v8_home = tmp_path / 'v8'
    with pytest.raises(module.V8BuildError, match='Unexpected V8 cache location'):
        module.prepare_restored_cache(config, tmp_path)


def test_linux_cache_validates_linkers_before_reuse(tmp_path, monkeypatch):
    module = _module()
    linker_dir = tmp_path / 'third_party' / 'llvm-build' / 'Release+Asserts' / 'bin'
    linker_dir.mkdir(parents=True)
    linker = linker_dir / 'ld.lld'
    linker.touch()
    config = SimpleNamespace(v8_home=tmp_path, output_dir=tmp_path / 'out',
                             _can_reuse_build=lambda: True)
    monkeypatch.setattr(module.os, 'access', lambda path, mode: True)
    with pytest.raises(module.V8BuildError, match='not executable: lld'):
        module.validate_cache(config, 'linux')

    (linker_dir / 'lld').touch()
    def broken_linker(command, *, check, timeout):
        assert command == [linker, '--version']
        raise module.subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(module.subprocess, 'run', broken_linker)
    with pytest.raises(module.subprocess.CalledProcessError):
        module.validate_cache(config, 'linux')
