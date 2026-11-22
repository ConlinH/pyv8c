"""Reference ownership is independent of a machine's Chrome installation."""
import contextlib
import sys
import threading
from types import SimpleNamespace

import pytest

from harness import ReferenceUnavailable, chrome_reference


@pytest.mark.parametrize("failure", ["", "launch", "body", "close"])
async def test_reference_cleanup_stays_on_its_creating_thread(monkeypatch, failure):
    events = []
    owner = threading.get_ident()

    def record(name):
        events.append((name, threading.get_ident()))

    async def close():
        record("close")
        if failure == "close":
            raise RuntimeError("close")

    async def launch(**_):
        record("launch")
        if failure == "launch":
            raise RuntimeError("launch")
        return SimpleNamespace(version="fixture", close=close)

    @contextlib.asynccontextmanager
    async def playwright():
        record("start")
        try:
            yield SimpleNamespace(chromium=SimpleNamespace(launch=launch))
        finally:
            record("stop")

    monkeypatch.setattr("harness.chrome_executable", lambda **_: ("fixture", ""))
    monkeypatch.setitem(sys.modules, "playwright.async_api", SimpleNamespace(async_playwright=playwright))
    expected = ReferenceUnavailable if failure == "launch" else RuntimeError
    with pytest.raises(expected) if failure else contextlib.nullcontext():
        async with chrome_reference():
            if failure == "body":
                raise RuntimeError("body")
    assert events == [(name, owner) for name in
                      (["start", "launch", "stop"] if failure == "launch" else
                       ["start", "launch", "close", "stop"])]
