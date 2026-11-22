"""Loopback host HTTP measurements for the benchmark runner."""

import asyncio
import json
import statistics
import threading
import time
from contextlib import ExitStack
from functools import wraps
from unittest.mock import patch


async def measure_host_http(engine):
    from pyv8c.async_api import Browser

    release = asyncio.Event()
    loaded = asyncio.Event()
    stalled = asyncio.Event()
    bulk_count = 0
    closed_peers = []

    async def serve(reader, writer):
        nonlocal bulk_count
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            path = headers.split(b" ", 2)[1]
            if path == b"/bulk":
                bulk_count += 1
                if bulk_count >= 2:
                    loaded.set()
                await release.wait()
            if path == b"/stall":
                stalled.set()
                closed_peers.append(await reader.read() == b"")
                return
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    def threads(prefix):
        return sum(item.name.startswith(prefix) for item in threading.enumerate())

    async def timed(page, url):
        start = time.perf_counter()
        assert (await page.net.get(url, timeout=5)).content == b"ok"
        return (time.perf_counter() - start) * 1_000

    listener = await asyncio.start_server(serve, "127.0.0.1", 0)
    async with listener, Browser(http_engine=engine, stub_logging="off") as browser:
        base = f"http://127.0.0.1:{listener.sockets[0].getsockname()[1]}"
        profile = browser.new_profile()
        pages = [profile.new_page() for _ in range(8)]
        await timed(pages[0], base + "/quick")
        serial = [await timed(pages[0], base + "/quick") for _ in range(32)]
        jobs = [asyncio.create_task(timed(page, base + "/bulk"))
                for page in pages for _ in range(2)]
        try:
            await asyncio.wait_for(loaded.wait(), 3)
            await asyncio.sleep(0.025)
            peak_wait_threads = threads("pyv8c-net_")
            first_wave = bulk_count
        finally:
            release.set()
            concurrent = await asyncio.gather(*jobs)
        timeout_request = asyncio.create_task(pages[0].net.get(base + "/stall", timeout=5))
        try:
            await asyncio.wait_for(stalled.wait(), 3)
            start = time.perf_counter()
            await browser.close()
            close_ms = (time.perf_counter() - start) * 1_000
        finally:
            await asyncio.gather(timeout_request, return_exceptions=True)
    return {
        "median": statistics.median(serial),
        "min": min(serial), "unit": "ms",
        "detail": f"{engine}; serial host HTTP with cold loopback connections",
        "serial_p95_ms": statistics.quantiles(serial, n=20, method="inclusive")[18],
        "bulk_p95_ms": statistics.quantiles(concurrent, n=20, method="inclusive")[18],
        "first_wave": first_wave, "peak_page_wait_threads": peak_wait_threads,
        "close_ms": close_ms, "transport_disconnected": closed_peers == [True],
    }


async def curl_host_http():
    return await measure_host_http("curl_cffi")


async def cronet_host_http():
    return await measure_host_http("cronet")


async def measure_resource_http(engine):
    from pyv8c.async_api import Browser

    async def serve(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
                         b"Content-Length: 2\r\nConnection: close\r\n\r\nok")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    listener = await asyncio.start_server(serve, "127.0.0.1", 0)
    async with listener, Browser(http_engine=engine, stub_logging="off") as browser:
        base = f"http://127.0.0.1:{listener.sockets[0].getsockname()[1]}"
        page = browser.new_page()
        await page.goto(base + "/", timeout=5)
        start = time.perf_counter()
        for _ in range(12):
            assert (await page.net.fetch("/page", timeout=5)).text == "ok"
        page_ms = (time.perf_counter() - start) * 1000 / 12
        source = ("onmessage=async()=>{const values=await Promise.all("
                  "Array.from({length:12},()=>fetch(location.origin+'/worker').then(r=>r.text())));"
                  "postMessage(values.join(''));};")
        page.evaluate("globalThis.answer='';globalThis.worker=new Worker(URL.createObjectURL(new Blob(["
                      + json.dumps(source) + "], {type:'text/javascript'})));"
                      "worker.onmessage=e=>answer=e.data;")
        start = time.perf_counter()
        page.evaluate("worker.postMessage(null)")
        await page.wait_for("answer.length===24", timeout=6)
        worker_ms = (time.perf_counter() - start) * 1000
        assert page.evaluate("answer") == "ok" * 12
    return {"median": worker_ms, "min": worker_ms, "unit": "ms",
            "detail": f"{engine}; Worker burst of 12; loopback",
            "page_request_ms": page_ms, "worker_burst_ms": worker_ms}


async def curl_resource_http():
    return await measure_resource_http("curl_cffi")


async def cronet_resource_http():
    return await measure_resource_http("cronet")


_HTTP_STAGES = (
    ("prepare_ms", "start", "submit"),
    ("admission_ms", "submit", "admitted"),
    ("dispatch_ms", "admitted", "transport_start"),
    ("transport_ms", "transport_start", "transport_done"),
    ("publish_ms", "transport_done", "posted"),
    ("owner_queue_ms", "posted", "owner"),
    ("realm_result_ms", "owner", "snapshot"),
    ("snapshot_ms", "snapshot", "decoded"),
    ("resume_ms", "decoded", "finish"),
    ("total_ms", "start", "finish"),
)


async def measure_http_stages(engine):
    """One in-flight request at a time so the timestamps need no request-ID map."""
    from pyv8c.async_api import Browser
    from pyv8c.platform import js_http
    from pyv8c.platform.resources.fetcher import _AsyncLoad

    responses = {}
    for size in (2, 1_048_576):
        headers = (f"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                   f"Content-Length: {size}\r\n\r\n").encode("ascii")
        responses[f"/{size}".encode("ascii")] = headers + b"x" * size
    connections = set()

    async def serve(reader, writer):
        connections.add(writer)
        try:
            while True:
                request = await reader.readuntil(b"\r\n\r\n")
                writer.write(responses[request.split(b" ", 2)[1]])
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            connections.discard(writer)
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    marks = {}

    def mark(name):
        marks[name] = time.perf_counter_ns()

    def observe(stack, target, name, before, after=None):
        original = getattr(target, name)

        @wraps(original)
        def record(*args, **kwargs):
            mark(before)
            result = original(*args, **kwargs)
            if after is not None:
                mark(after)
            return result

        stack.enter_context(patch.object(target, name, record))

    listener = await asyncio.start_server(serve, "127.0.0.1", 0)
    try:
        async with listener, Browser(http_engine=engine, ws_engine=None, cache=False,
                                     stub_logging="off") as browser:
            address = f"http://127.0.0.1:{listener.sockets[0].getsockname()[1]}"
            page = browser.new_page()
            await page.set_content("<p>local</p>", address + "/")
            backend = page.profile.network_backend
            transport = backend.http
            original = transport._single_call

            @wraps(original)
            async def transport_call(request):
                mark("transport_start")
                response = await original(request)
                mark("transport_done")
                return response

            workloads = {}
            with ExitStack() as stack:
                for target, name, before, after in (
                    (backend, "submit", "submit", None),
                    (backend, "_start", "admitted", None),
                    (_AsyncLoad, "enqueue", "posted", None),
                    (_AsyncLoad, "settle", "owner", None),
                    (js_http, "_answer", "snapshot", "decoded"),
                ):
                    observe(stack, target, name, before, after)
                stack.enter_context(patch.object(transport, "_single_call", transport_call))
                for kind in ("fetch", "xhr"):
                    call = getattr(page.net, kind)
                    for size in (2, 1_048_576):
                        url = f"/{size}"
                        assert len((await call(url, timeout=5)).text) == size
                        samples = []
                        for _ in range(8):
                            marks.clear()
                            mark("start")
                            response = await call(url, timeout=5)
                            mark("finish")
                            assert len(response.text) == size
                            samples.append({name: (marks[end] - marks[start]) / 1_000_000
                                            for name, start, end in _HTTP_STAGES})
                        workloads[f"{kind}_{size}"] = {
                            "median_ms": {name: statistics.median(item[name] for item in samples)
                                          for name, _, _ in _HTTP_STAGES},
                            "p95_total_ms": statistics.quantiles(
                                [item["total_ms"] for item in samples], n=20, method="inclusive")[18],
                            "samples": samples,
                        }
    finally:
        for writer in tuple(connections):
            writer.close()
    totals = [item["total_ms"] for item in workloads["fetch_1048576"]["samples"]]
    return {"median": statistics.median(totals), "min": min(totals), "unit": "ms",
            "workload": "http-stages-keepalive-v1", "detail": f"{engine}; 1 MiB Fetch; loopback",
            "stages": workloads}


async def curl_http_stages():
    return await measure_http_stages("curl_cffi")


async def cronet_http_stages():
    return await measure_http_stages("cronet")


async def cronet_completion_http():
    """Measure completion independently of the framework and V8."""
    from cycronet import AsyncCronetClient

    marks = {}
    connections = set()
    headers = b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\n"

    async def serve(reader, writer):
        connections.add(writer)
        try:
            while True:
                request = await reader.readuntil(b"\r\n\r\n")
                marks["headers_written"] = time.perf_counter_ns()
                if request.split(b" ", 2)[1] == b"/split":
                    writer.write(headers + b"ab")
                    await writer.drain()
                    await asyncio.sleep(.05)
                    marks["body_written"] = time.perf_counter_ns()
                    writer.write(b"cd")
                else:
                    marks["body_written"] = marks["headers_written"]
                    writer.write(headers + b"abcd")
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            connections.discard(writer)
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    listener = await asyncio.start_server(serve, "127.0.0.1", 0)
    address = f"http://127.0.0.1:{listener.sockets[0].getsockname()[1]}"
    workloads = {}
    async with listener:
        client = AsyncCronetClient()
        try:
            for path in ("immediate", "split"):
                samples = []
                for _ in range(8):
                    response = await client.get(f"{address}/{path}", stream=True, allow_redirects=False)
                    received_headers = time.perf_counter_ns()
                    data = []
                    try:
                        async for chunk in response.aiter_content():
                            if not data:
                                first_chunk = time.perf_counter_ns()
                            data.append(chunk)
                        eof = time.perf_counter_ns()
                    finally:
                        response.close()
                    closed = time.perf_counter_ns()
                    assert b"".join(data) == b"abcd"
                    samples.append({
                        "headers_ms": (received_headers - marks["headers_written"]) / 1_000_000,
                        "first_chunk_ms": (first_chunk - marks["headers_written"]) / 1_000_000,
                        "eof_after_body_ms": (eof - marks["body_written"]) / 1_000_000,
                        "stream_close_ms": (closed - eof) / 1_000_000,
                    })
                workloads[path] = samples[1:]
            buffered = []
            for _ in range(8):
                response = await client.get(f"{address}/immediate", allow_redirects=False)
                completed = time.perf_counter_ns()
                assert response.content == b"abcd"
                buffered.append((completed - marks["body_written"]) / 1_000_000)
            workloads["buffered"] = buffered[1:]
        finally:
            await client.close()
            for writer in tuple(connections):
                writer.close()
    delays = workloads["buffered"]
    return {"median": statistics.median(delays), "min": min(delays), "unit": "ms",
            "workload": "cronet-sdk-completion-v1", "detail": "Cronet SDK; last byte to buffered result; loopback",
            "stages": workloads}
