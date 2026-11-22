
import asyncio
import json
import threading
import pytest
from pyv8c.async_api import Browser
from pyv8c.web.workers.runtime import WorkerRuntime


@pytest.mark.parametrize("phase", ["timer", "microtask", "frame", "frame-microtask"])
async def test_worker_background_execution_uses_a_budget_and_recovers(monkeypatch, phase):
    started = threading.Event()
    runtimes = []
    original = WorkerRuntime._started
    def ready(runtime):
        runtime.executor.default_timeout = 0.03
        runtimes.append(runtime)
        original(runtime)
        started.set()
    monkeypatch.setattr(WorkerRuntime, '_started', ready)
    body = "const start=Date.now();while(Date.now()-start<150){};postMessage('overran');"
    if phase in ('microtask', 'frame-microtask'):
        body = "queueMicrotask(()=>{" + body + "});"
    source = "setTimeout(()=>{" + body + "},0);setTimeout(()=>postMessage('after'),0);"
    if phase.startswith('frame'):
        source = ("requestAnimationFrame(()=>{" + body + "});"
                  "setTimeout(()=>requestAnimationFrame(()=>postMessage('after')),75);")
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        page.evaluate("window.messages=[];window.worker=new Worker(URL.createObjectURL(new Blob(["
                      + json.dumps(source) + "])));worker.onmessage=e=>messages.push(e.data)")
        assert (await asyncio.to_thread(started.wait, 1))
        (await page.wait_for("messages.includes('after')", timeout=2))
        assert page.evaluate('messages') == ['after']
        assert runtimes[0].executor.call(lambda: 42, timeout=1) == 42
    finally:
        (await browser.close())
        for runtime in runtimes:
            (await asyncio.to_thread(runtime.executor._thread.join, 2))
            assert not runtime.executor._thread.is_alive()


async def test_parent_termination_interrupts_the_active_worker_operation(monkeypatch):
    ready = threading.Event()
    runtimes = []
    original = WorkerRuntime._started
    def started(runtime):
        runtime.executor.default_timeout = None
        runtimes.append(runtime)
        original(runtime)
        ready.set()
    monkeypatch.setattr(WorkerRuntime, '_started', started)
    source = "setTimeout(()=>{postMessage('entered');let s=Date.now();while(Date.now()-s<2000){}},0)"
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        page.evaluate("window.entered=false;window.worker=new Worker(URL.createObjectURL(new Blob(["
                      + json.dumps(source) + "])));worker.onmessage=()=>entered=true")
        assert (await asyncio.to_thread(ready.wait, 1))
        (await page.wait_for('entered', timeout=1))
        page.evaluate('worker.terminate()')
        runtime = runtimes[0]
        (await asyncio.to_thread(runtime.executor._thread.join, 0.5))
        assert not runtime.executor._thread.is_alive()
        assert page.evaluate('1+1') == 2
    finally:
        (await browser.close())
        for runtime in runtimes:
            (await asyncio.to_thread(runtime.executor._thread.join, 3))
