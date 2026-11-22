import gc
import weakref

import pytest

pyv8 = pytest.importorskip('pyv8c.pyv8')
from pyv8c.async_api import Browser


def collect():
    for _ in range(3):
        pyv8.low_memory_notification()
        gc.collect()


async def test_closed_context_is_removed_from_browser():
    browser = (await Browser(stub_logging='off').__aenter__())
    refs = []
    try:
        for _ in range(20):
            context = browser.new_profile()
            refs.append(weakref.ref(context))
            (await context.close())
            del context
        collect()
        assert browser.profiles == []
        assert not any(ref() is not None for ref in refs)
    finally:
        (await browser.close())


async def test_closed_pages_are_collectable_with_chrome_installed():
    browser = (await Browser(stub_logging='off').__aenter__())
    context = browser.new_profile()
    refs = []
    try:
        for _ in range(20):
            page = context.new_page()
            assert page.evaluate('typeof chrome.csi') == 'function'
            refs.append(weakref.ref(page))
            (await page.close())
            del page
        collect()
        assert context.pages == []
        assert not any(ref() is not None for ref in refs)
    finally:
        (await browser.close())


async def test_browser_close_closes_every_context():
    browser = (await Browser(stub_logging='off').__aenter__())
    contexts = [browser.new_profile() for _ in range(4)]
    (await browser.close())
    assert all(context.closed for context in contexts)
    assert browser.profiles == []


async def test_page_cleanup_failure_can_be_retried(monkeypatch):
    browser = (await Browser(stub_logging='off').__aenter__())
    context = browser.new_profile()
    page = context.new_page()
    original = page.net._retire
    calls = []

    def fail_once():
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError('temporary close failure')
        original()

    monkeypatch.setattr(page.net, '_retire', fail_once)
    try:
        with pytest.raises(RuntimeError, match='temporary close failure'):
            (await page.close())
        assert not page.closed
        assert not page.realm.executor.closed
        assert page.tasks.closed
        assert page in context.pages
        (await page.close())
        assert page.closed
        assert page not in context.pages
    finally:
        (await browser.close())


async def test_browser_cleanup_attempts_other_contexts_and_can_retry(monkeypatch):
    browser = (await Browser(stub_logging='off').__aenter__())
    first, second = browser.new_profile(), browser.new_profile()
    original = first.close
    calls = []

    async def fail_once():
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError('temporary close failure')
        await original()

    monkeypatch.setattr(first, 'close', fail_once)
    with pytest.raises(RuntimeError, match='temporary close failure'):
        (await browser.close())
    assert second.closed
    assert browser.profiles == [first]
    (await browser.close())
    assert first.closed
    assert browser.profiles == []


async def test_worker_isolate_failure_preserves_retry_state():
    from types import SimpleNamespace
    from pyv8c.platform.realms import WorkerRealm

    calls = []

    class Isolate:
        def close(self):
            calls.append('isolate')
            if calls.count('isolate') == 1:
                raise RuntimeError('persistent handle')

    session = SimpleNamespace(detach=lambda: calls.append('detach'))
    from pyv8c.foundation.background import BackgroundWork
    config = SimpleNamespace(
        session=session,
        resource_session=SimpleNamespace(
            profile=SimpleNamespace(network_backend=None), site_for_cookies="null",
        ),
        environment=None,
        background=BackgroundWork(),
        browser=SimpleNamespace(gpu_enabled=False, _graphics_backend=None),
        cross_origin_isolated=False,
        denied_permissions=(),
    )
    realm = WorkerRealm(None, config)
    realm.v8_context = SimpleNamespace(close=lambda: calls.append('context'))
    realm.isolate = Isolate()
    with pytest.raises(RuntimeError, match='persistent handle'):
        realm.close()
    assert realm.v8_context is None
    assert realm.isolate is not None
    assert calls[:3] == ['context', 'detach', 'isolate']
    realm.close()
    assert realm.isolate is None
