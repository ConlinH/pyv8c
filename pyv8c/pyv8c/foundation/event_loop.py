"""Create a consistent, high-resolution owner event loop before scheduling."""

import asyncio
import time


def create_event_loop():
    """Choose the clock before any handles exist; never retime a running loop."""
    loop = asyncio.new_event_loop()
    loop.time = time.perf_counter
    loop._clock_resolution = time.get_clock_info("perf_counter").resolution
    return loop


def run(coroutine):
    with asyncio.Runner(loop_factory=create_event_loop) as runner:
        return runner.run(coroutine)
