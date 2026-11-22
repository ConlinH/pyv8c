import os
import asyncio
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYV8_ROOT = ROOT.parent / "pyv8"
NATIVE_DIR = ROOT / "pyv8c" / "pyv8"


def _use_installed_wheel():
    """A clean CI checkout carries no compiled extension; use the installed wheel.

    Locally the source tree normally has ``_pyv8.pyd`` / ``_pyv8.so`` and keeps
    the original source-based run.  ``PYV8C_NATIVE_SMOKE_INSTALLED`` forces
    either branch explicitly so both can be verified.
    """
    override = os.environ.get("PYV8C_NATIVE_SMOKE_INSTALLED")
    if override is not None:
        return override.strip().lower() not in ("", "0", "false", "no")
    return not any(NATIVE_DIR.glob("_pyv8*.pyd")) and not any(NATIVE_DIR.glob("_pyv8*.so"))


def _run(case):
    env = os.environ.copy()
    if _use_installed_wheel():
        # ``python -c`` puts its working directory on sys.path.  Keep that and
        # PYTHONPATH away from the checkout so the child imports the wheel the
        # CI job just installed, not an extension-less source package.
        env.pop("PYTHONPATH", None)
        cwd = tempfile.gettempdir()
    else:
        paths = [str(PYV8_ROOT), str(ROOT)]
        env["PYTHONPATH"] = os.pathsep.join(
            paths + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
        )
        cwd = str(ROOT)
    code = f"""
import faulthandler
import os
faulthandler.enable()
from pyv8c.async_api import Browser, run
async def main():
    async with Browser(idl_path=os.environ.get("PYV8C_TEST_IDL_PATH")) as runtime:
        c = runtime.new_profile()
        p = c.new_page()
        print('before-content', flush=True)
        p.set_content({case!r}, url='http://example.test/')
        print('after-content', flush=True)
        value = p.evaluate("document.querySelector('#x') && document.querySelector('#x') === document.querySelector('#x')")
        print('after-evaluate', value, flush=True)
        p.close()
        c.close()
    print('closed', flush=True)
run(main())
"""
    return subprocess.run(
        [sys.executable, "-X", "faulthandler", "-c", code],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=30,
    )


async def test_native_lifecycle_smoke_cases():
    # Two variants cover both parse paths; the point is a crash-free child process.
    for html in ("", "<div id='x' class='a'>text</div>"):
        result = await asyncio.to_thread(_run, html)
        assert result.returncode == 0, (html, result.returncode, result.stdout, result.stderr)
        assert "closed" in result.stdout, (html, result.stdout, result.stderr)
