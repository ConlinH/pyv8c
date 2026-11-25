"""Failures must not turn unknown or dirty V8 state into a successful build."""
from pathlib import Path
import sys
from unittest.mock import patch

import pytest

from tests.test_v8_build import v8_build


def test_icu_patch_is_temporary_and_part_of_build_identity(tmp_path, monkeypatch):
    monkeypatch.delenv('GIT_CONFIG_COUNT', raising=False)
    (tmp_path / 'V8_VERSION').write_text('15.4.68')
    config = v8_build.V8BuildConfig.from_environment(tmp_path, {})
    paths = ('third_party/icu/source/i18n/timezone.cpp', 'third_party/icu/source/common/locid.cpp')
    patch_file = tmp_path / 'icu.patch'
    hunks = []
    for name in paths:
        source = config.v8_home / name
        source.parent.mkdir(parents=True)
        source.write_bytes(b'original\n')
        hunks.append(f'--- a/{name}\n+++ b/{name}\n@@ -1 +1 @@\n-original\n+patched\n')
    patch_file.write_bytes(''.join(hunks).encode())
    monkeypatch.setattr(v8_build, 'ICU_DATETIME_PATCH', patch_file)
    stamp = config._stamp_data()
    with pytest.raises(RuntimeError, match='build failed'):
        with config._icu_datetime_patch():
            assert all((config.v8_home / name).read_bytes() == b'patched\n' for name in paths)
            raise RuntimeError('build failed')
    assert all((config.v8_home / name).read_bytes() == b'original\n' for name in paths)
    patch_file.write_bytes(b'incompatible\n')
    assert config._stamp_data() != stamp
    with pytest.raises(v8_build.V8BuildError, match='git apply'):
        with config._icu_datetime_patch():
            pytest.fail('invalid patch reached build')


def test_selected_windows_sdk_is_applied_temporarily(tmp_path, monkeypatch):
    (tmp_path / 'V8_VERSION').write_text('15.4.68')
    config = v8_build.V8BuildConfig.from_environment(tmp_path, {})
    sdk = tmp_path / 'sdk'
    version = '10.0.26100.0'
    for relative in ('Include/um', 'Include/shared', 'Include/ucrt', 'Lib/um/x64', 'Lib/ucrt/x64'):
        category, rest = relative.split('/', 1)
        (sdk / category / version / rest).mkdir(parents=True)
    toolchain = config.v8_home / 'build/toolchain/win/setup_toolchain.py'
    toolchain.parent.mkdir(parents=True)
    original = b"SDK_VERSION = '10.0.28000.0'\n"
    toolchain.write_bytes(original)
    build_gn = config.v8_home / 'build/config/win/BUILD.gn'
    build_gn.parent.mkdir(parents=True)
    original_gn = b'"NTDDI_VERSION=NTDDI_WIN11_BR",\n'
    build_gn.write_bytes(original_gn)
    monkeypatch.setenv('PYV8_WINDOWS_SDK', str(sdk))
    monkeypatch.setenv('PYV8_WINDOWS_SDK_VERSION', version)
    with config._source_compatibility_patches():
        assert toolchain.read_bytes() == b"SDK_VERSION = '10.0.26100.0'\n"
        assert b'NTDDI_VERSION=NTDDI_WIN11_GE' in build_gn.read_bytes()
    assert toolchain.read_bytes() == original
    assert build_gn.read_bytes() == original_gn


def test_checkout_failure_is_not_silently_bypassed(tmp_path, monkeypatch):
    (tmp_path / 'V8_VERSION').write_text('15.4.68')
    config = v8_build.V8BuildConfig.from_environment(tmp_path, {'PYV8_BUILD_V8': '1'})
    temporal = config.v8_home / 'src/objects/js-temporal-objects.cc'
    temporal.parent.mkdir(parents=True)
    temporal.write_text('fixture')
    monkeypatch.setattr(config, '_ensure_depot_tools', lambda: None)
    monkeypatch.setattr(config, '_bootstrap_depot_tools', lambda: None)
    def fail():
        raise v8_build.V8BuildError('checkout must be clean')
    monkeypatch.setattr(config, '_checkout_v8', fail)
    monkeypatch.setattr(config, '_build_v8', lambda: pytest.fail('must not build'))
    with pytest.raises(v8_build.V8BuildError, match='must be clean'):
        config.prepare()


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows-specific V8 layout patches')
def test_patch_setup_failure_restores_earlier_files(tmp_path, monkeypatch):
    (tmp_path / 'V8_VERSION').write_text('15.4.68')
    config = v8_build.V8BuildConfig.from_environment(tmp_path, {})
    directory = config.v8_home / 'src/objects'
    directory.mkdir(parents=True)
    backing = directory / 'backing-store.cc'
    original = b'return isolate->heap()->allocator()->RetryCustomAllocate(\n        fn, internal::AllocationType::kOld,\n'
    backing.write_bytes(original)
    atomics = directory / 'js-atomics-synchronization.h'
    atomics.write_bytes(b'  ExternalPointerMember<kWaiterQueueNodeTag> waiter_queue_head_;\n  std::atomic<uint32_t> state_;\n')
    write = Path.write_bytes
    def fail_second(path, data):
        if path == atomics:
            raise OSError('simulated write failure')
        return write(path, data)
    with patch.object(Path, 'write_bytes', fail_second):
        with pytest.raises(OSError, match='simulated write failure'):
            with config._source_compatibility_patches():
                pytest.fail('must not reach body')
    assert backing.read_bytes() == original


def test_microtask_abi_requires_evidence_or_explicit_choice(tmp_path, monkeypatch):
    monkeypatch.delenv('PYV8_CPPGC_MICROTASK_QUEUE', raising=False)
    with pytest.raises(v8_build.V8BuildError, match='cannot determine'):
        v8_build.cppgc_microtask_queue_enabled(tmp_path)
    monkeypatch.setenv('PYV8_CPPGC_MICROTASK_QUEUE', '0')
    assert v8_build.cppgc_microtask_queue_enabled(tmp_path) is False
    obj = tmp_path / 'obj'
    obj.mkdir()
    (obj / 'v8_base.ninja').write_text('defines = -DV8_ENABLE_WEBASSEMBLY\n')
    assert v8_build.cppgc_microtask_queue_enabled(tmp_path) is False
    (obj / 'v8_base.ninja').write_text('defines = -DV8_CPPGC_MICROTASK_QUEUE\n')
    with pytest.raises(v8_build.V8BuildError, match='conflicts'):
        v8_build.cppgc_microtask_queue_enabled(tmp_path)
    monkeypatch.delenv('PYV8_CPPGC_MICROTASK_QUEUE')
    assert v8_build.cppgc_microtask_queue_enabled(tmp_path) is True
