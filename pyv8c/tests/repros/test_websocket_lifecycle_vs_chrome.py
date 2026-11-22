"""Local WebSocket lifecycle and task checkpoints, compared serially."""

import asyncio
import pytest
import threading

pytest.importorskip('pyv8c.pyv8')
pytest.importorskip('websockets')

from websockets.sync.server import serve

from harness import chrome_reference, ReferenceUnavailable
from pyv8c.async_api import Browser


async def test_connecting_close_events_and_microtasks_match_chrome():
    script = """globalThis.events=[];
        const ws=new WebSocket('ws://127.0.0.1:1/');
        ws.onopen=()=>events.push('open');
        ws.onerror=()=>{events.push(['error',ws.readyState]);queueMicrotask(()=>events.push('microtask'));};
        ws.onclose=e=>events.push(['close',e.code,e.wasClean,ws.readyState]);
        ws.close();
        events.push(['immediate',ws.readyState]);
    """
    try:
        async with chrome_reference() as reference:
            chrome = (await reference.browser.new_page())
            browser = (await Browser(ws_engine='websockets', stub_logging='off').__aenter__())
            try:
                page = browser.new_profile().new_page()
                (await chrome.evaluate(script))
                (await chrome.wait_for_function('events.length===4'))
                page.evaluate(script)
                (await page.wait_for('events.length===4', timeout=5))
                assert page.evaluate('events') == (await chrome.evaluate('events'))
            finally:
                (await browser.close())
                (await chrome.close())
    except ReferenceUnavailable as error:
        pytest.skip(str(error))


async def test_buffered_bytes_task_boundaries_and_send_after_close_match_chrome():
    received = []

    def echo(connection):
        messages = []
        for message in connection:
            messages.append(message)
            connection.send(message)
        received.append(messages)

    with serve(echo, '127.0.0.1', 0) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.socket.getsockname()[1]
        script = f"""globalThis.events=[];globalThis.done=false;
            const ws=new WebSocket('ws://127.0.0.1:{port}/');
            let messages=0;
            ws.onmessage=()=>messages++;
            ws.onopen=()=>{{
                ws.send('中');ws.send('😀');ws.send(new Blob(['ab']));
                ws.send(String.fromCharCode(0xd800));
                events.push(['open',ws.bufferedAmount]);
                queueMicrotask(()=>events.push(['micro',ws.bufferedAmount]));
                const timer=setInterval(()=>{{
                    if(messages!==4 || ws.bufferedAmount!==0) return;
                    clearInterval(timer);ws.close(1000,'done');ws.send('尾');
                    events.push(['closing',ws.bufferedAmount]);
                }},1);
            }};
            ws.onclose=()=>{{events.push(['closed',ws.bufferedAmount]);
                ws.send('🙂');events.push(['after',ws.bufferedAmount]);done=true;}};
            'started';
        """
        try:
            try:
                async with chrome_reference() as reference:
                    chrome = (await reference.browser.new_page())
                    browser = (await Browser(ws_engine='websockets', stub_logging='off').__aenter__())
                    try:
                        page = browser.new_profile().new_page()
                        (await chrome.evaluate(script))
                        (await chrome.wait_for_function('done', timeout=10000))
                        page.evaluate(script)
                        (await page.wait_for('done', timeout=10))
                        assert page.evaluate('events') == (await chrome.evaluate('events')) == [
                            ['open', 12], ['micro', 12], ['closing', 3], ['closed', 3], ['after', 7],
                        ]
                    finally:
                        (await browser.close())
                        (await chrome.close())
            except ReferenceUnavailable as error:
                pytest.skip(str(error))
        finally:
            server.shutdown()
            (await asyncio.to_thread(thread.join, 5))
            assert not thread.is_alive()
        assert received == [['中', '😀', b'ab', '\ufffd']] * 2
