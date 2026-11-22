"""Shared fixtures for the pyv8c suite.

This module must stay importable *without* the native :mod:`pyv8` extension:
``tests/unit`` is expected to run in environments that only have the Chromium
metadata tooling.  Every runtime fixture therefore performs its import lazily
and skips through :func:`pytest.importorskip` when the extension is missing.
Setting ``PYV8C_OFFLINE=1`` hides the extension package at import time, so a
clean checkout (package present, compiled binary absent) skips the same cases
as the policy gate instead of re-raising a broken ``from . import _pyv8``.

Nothing here is mandatory.  A test module that already declares a local
``browser``/``page`` fixture keeps its own definition -- the closer fixture
wins -- so adopting these helpers is incremental rather than all-or-nothing.
"""
from __future__ import annotations

import importlib.abc
import os
import sys
import threading
import time
from urllib.parse import urlsplit

import pytest

if os.environ.get("PYV8C_OFFLINE") == "1":
    # ``pytest.importorskip`` only skips when the ImportError names the module
    # it asked for.  A checkout without the compiled binary instead raises
    # ``cannot import name '_pyv8'`` from inside ``pyv8c.pyv8``, which pytest
    # re-raises as a collection error.  Hiding the package at the finder level
    # gives local runs, ``tools/check_policy.py`` and CI one identical rule.
    class _NoNativeExtension(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if (fullname in ("pyv8c.pyv8", "pyv8c._native._native")
                    or fullname.startswith("pyv8c.pyv8.")):
                raise ModuleNotFoundError(
                    "pyv8c.pyv8 deliberately unavailable (PYV8C_OFFLINE)",
                    name=fullname,
                )

    sys.meta_path.insert(0, _NoNativeExtension())

ORIGIN = "http://127.0.0.1:9"

#: Options every fixture-backed browser shares.  Stub logging floods the
#: capture buffer and makes assertion output unreadable, so it stays off
#: unless a test explicitly asks for it.
QUIET_BROWSER_OPTIONS = {"stub_logging": "off"}


def require_pyv8():
    """Skip the calling test when the native extension is not importable."""
    return pytest.importorskip("pyv8c.pyv8", reason="native pyv8 extension is not importable")


def default_loader(url, request):
    """Offline loader used by fixtures that do not care about responses.

    Serves an empty 200 document for every URL so a page can navigate and
    reach ``load`` without touching the network.
    """
    path = urlsplit(str(url)).path
    if path.endswith(".js"):
        return {
            "status": 200,
            "url": str(url),
            "body": "",
            "headers": {"content-type": "text/javascript"},
        }
    return {"status": 200, "url": str(url), "body": "", "headers": {"content-type": "text/html"}}


async def make_browser(**options):
    """Build a quiet, offline :class:`~pyv8c.async_api.Browser`.

    ``resource_loader`` defaults to :func:`default_loader`; pass an explicit
    one to serve fixtures.  Unknown keywords are forwarded to ``Browser``.
    """
    require_pyv8()
    from pyv8c.async_api import Browser

    options = {**QUIET_BROWSER_OPTIONS, **options}
    options.setdefault("resource_loader", default_loader)
    return (await Browser(**options).__aenter__())


@pytest.fixture
async def browser():
    """A quiet offline Browser, closed on teardown."""
    instance = (await make_browser())
    try:
        yield instance
    finally:
        (await instance.close())


@pytest.fixture
async def page(browser):
    """A fresh Page on ``about:blank``, closed before its Browser."""
    instance = browser.new_profile().new_page()
    try:
        yield instance
    finally:
        (await instance.close())


@pytest.fixture(scope="module")
async def shared_browser():
    """A quiet offline Browser shared by a module."""
    instance = (await make_browser())
    try:
        yield instance
    finally:
        (await instance.close())


@pytest.fixture(scope="module")
async def shared_page(shared_browser):
    """One Page for a whole module: only for cases that never write to it."""
    instance = shared_browser.new_profile().new_page()
    try:
        yield instance
    finally:
        (await instance.close())


@pytest.fixture
def thread_leak_check():
    """Assert the process returns to its baseline thread count.

    Yields a callable ``check(tolerance=1, timeout=10.0)``.  Threads started by
    a realm executor, the DevTools server or the network pool are daemonised
    and may take a moment to unwind, so the check polls until the deadline
    instead of sampling once.
    """
    baseline = threading.active_count()

    def check(tolerance=1, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and threading.active_count() > baseline + tolerance:
            time.sleep(0.05)
        leaked = threading.active_count() - baseline
        assert leaked <= tolerance, (
            f"{leaked} threads leaked (baseline {baseline}, tolerance {tolerance}); "
            f"live: {sorted(t.name for t in threading.enumerate())}"
        )
        return leaked

    yield check





def pytest_addoption(parser):
    """Select a generated baseline explicitly without replacing the shipped archive."""
    import os
    parser.addoption("--idl-path", default=os.environ.get("PYV8C_TEST_IDL_PATH"),
                     help="IDL document used by runtime tests and their subprocesses")


def pytest_configure(config):
    selected = config.getoption("--idl-path")
    if selected:
        import os
        from pathlib import Path
        from pyv8c.engine import idl
        target = Path(selected).resolve()
        idl.DEFAULT_IDL_PATH = target
        os.environ["PYV8C_TEST_IDL_PATH"] = str(target)


def pytest_asyncio_loop_factories():
    from pyv8c.foundation.event_loop import create_event_loop
    return {"runtime": create_event_loop}
