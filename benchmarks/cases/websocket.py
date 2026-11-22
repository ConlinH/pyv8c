"""Loopback WebSocket benchmark. Run with python -m benchmarks.cases.websocket."""
import argparse
import asyncio
import json
import math
import threading
import time

import websockets
from loguru import logger
from websockets.asyncio.server import serve

from pyv8c.async_api import Browser


def rss_mib():
    try:
        import psutil
    except ImportError:
        return None
    return round(psutil.Process().memory_info().rss / (1024 * 1024), 2)


async def echo(connection):
    async for message in connection:
        await connection.send(message)


async def measure(url, connections, messages):
    async with Browser(http_engine=None) as browser:
        page = browser.new_page()
        await page.set_content()
        initial_threads = threading.active_count()
        initial_rss = rss_mib()
        started = time.perf_counter()
        page.evaluate(f"""
            globalThis.samples = [];
            globalThis.failures = 0;
            globalThis.openCount = 0;
            globalThis.sockets = Array.from({{length: {connections}}}, () => {{
                const socket = new WebSocket({json.dumps(url)});
                socket.binaryType = 'arraybuffer';
                let sentAt = 0;
                let count = 0;
                const send = () => {{
                    sentAt = performance.now();
                    socket.send(count % 2 ? new Uint8Array(1024) : 'x'.repeat(256));
                }};
                socket.onopen = () => {{ openCount++; send(); }};
                socket.onerror = () => failures++;
                socket.onmessage = () => {{
                    samples.push(performance.now() - sentAt);
                    if (++count < {messages}) send();
                }};
                return socket;
            }});
            void 0;
        """)
        await page.wait_for(f"openCount === {connections} || failures > 0", timeout=30)
        open_ms = (time.perf_counter() - started) * 1000
        await page.wait_for(f"samples.length === {connections * messages} || failures > 0", timeout=30)
        wall_ms = (time.perf_counter() - started) * 1000
        assert page.evaluate("failures") == 0, "one or more sockets failed"
        samples = sorted(page.evaluate("samples"))
        connected_rss = rss_mib()
        thread_delta = threading.active_count() - initial_threads
        closing = time.perf_counter()
        await browser.close()
        close_ms = (time.perf_counter() - closing) * 1000
    return {
        "connections": connections,
        "round_trips": len(samples),
        "client_threads_added": thread_delta,
        "rss_added_mib": round(connected_rss - initial_rss, 2) if initial_rss is not None else None,
        "open_ms": round(open_ms, 2),
        "total_ms": round(wall_ms, 2),
        "rtt_p50_ms": round(samples[math.ceil(len(samples) * .5) - 1], 2),
        "rtt_p95_ms": round(samples[math.ceil(len(samples) * .95) - 1], 2),
        "close_ms": round(close_ms, 2),
    }


async def run(args):
    async with serve(echo, "127.0.0.1", 0) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/"
        for connections in args.connections:
            for repeat in range(args.repeat):
                result = await measure(url, connections, args.messages)
                print(json.dumps({"websockets": websockets.__version__, "run": repeat + 1, **result}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connections", type=int, nargs="+", default=[1, 32, 128])
    parser.add_argument("--messages", type=int, default=20, help="round trips per connection")
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args()
    if min(*args.connections, args.messages, args.repeat) < 1:
        parser.error("connection counts, messages and repeats must be positive")
    logger.remove()
    asyncio.run(run(args))
