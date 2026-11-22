"""Validate restored V8 outputs and discard incomplete CI artifacts."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

NATIVE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(NATIVE_ROOT))

from build_support import _snapshot_link_inputs
from v8_build import V8BuildConfig, V8BuildError, cppgc_microtask_queue_enabled


def validate_cache(config, platform=sys.platform):
    if not config._can_reuse_build():
        raise V8BuildError('V8 build stamp, headers, library or ICU data is missing or incompatible')
    llvm = config.v8_home / 'third_party' / 'llvm-build' / 'Release+Asserts'
    if platform == 'linux':
        for tool in ('ld.lld', 'lld'):
            linker = llvm / 'bin' / tool
            if not linker.is_file() or not os.access(linker, os.X_OK):
                raise V8BuildError(f'V8 LLVM linker is missing or not executable: {tool}')
        subprocess.run([llvm / 'bin' / 'ld.lld', '--version'], check=True, timeout=15)
    cppgc_microtask_queue_enabled(config.output_dir)
    if platform != 'win32':
        return
    for tool in ('clang-cl.exe', 'lld-link.exe'):
        if not (llvm / 'bin' / tool).is_file():
            raise V8BuildError(f'V8 LLVM tool is missing: {tool}')
    if not (config.output_dir / 'obj' / 'mksnapshot.ninja').is_file():
        raise V8BuildError('V8 mksnapshot link metadata is missing')
    if not _snapshot_link_inputs(config.output_dir):
        raise V8BuildError('V8 mksnapshot Rust link inputs are missing')


CACHE_ERRORS = (OSError, ValueError, V8BuildError, subprocess.SubprocessError)


def prepare_restored_cache(config, native_root):
    """Return whether CI can reuse its restored artifacts."""
    home = config.v8_home
    expected = native_root / '.pyv8-build' / 'versions' / config.version / 'v8'
    if home != expected or not home.resolve().is_relative_to(native_root):
        raise V8BuildError(f'Unexpected V8 cache location: {home}')
    for component in (home, *home.parents):
        if component == native_root:
            break
        if component.is_symlink() or component.is_junction():
            raise V8BuildError(f'V8 cache path crosses a link: {component}')
    if not home.exists():
        return False
    try:
        validate_cache(config)
    except CACHE_ERRORS as error:
        if (home / '.git').exists() or (home / '.git').is_symlink():
            raise V8BuildError(f'Refusing to discard a V8 checkout: {home}') from error
        print(f'Discarding invalid V8 cache: {error}', file=sys.stderr)
        shutil.rmtree(home)
        return False
    print(f'V8 cache validated: {config.version}')
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--restore', action='store_true',
                        help='discard an invalid restored CI artifact and report reuse')
    args = parser.parse_args()
    try:
        config = V8BuildConfig.from_environment(NATIVE_ROOT, download_and_build=True)
        if args.restore:
            reusable = prepare_restored_cache(config, NATIVE_ROOT)
            with Path(os.environ['GITHUB_OUTPUT']).open('a', encoding='utf-8') as output:
                print(f'reusable={str(reusable).lower()}', file=output)
            return 0
        validate_cache(config)
    except CACHE_ERRORS as error:
        print(f'V8 cache is invalid: {error}', file=sys.stderr)
        return 1
    print(f'V8 cache validated: {config.version}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
