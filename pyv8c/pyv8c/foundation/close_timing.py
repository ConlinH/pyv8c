"""Close phase diagnostics shared by the runtime and its transports."""

from contextlib import contextmanager
from time import perf_counter

from .logging import logger


@contextmanager
def close_phase(owner, phase):
    started = perf_counter()
    status = "finished"
    try:
        yield
    except BaseException:
        status = "failed"
        raise
    finally:
        elapsed = perf_counter() - started
        logger.bind(close_timing=True).log(
            "WARNING" if elapsed >= 0.25 else "DEBUG",
            "close {}#{:x} {} {} in {:.3f}s", type(owner).__name__, id(owner), phase, status, elapsed,
        )
