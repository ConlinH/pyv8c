"""Lossless concurrent-Future delivery to an asyncio owner."""

import asyncio
import weakref


def consume_future(future):
    if not future.cancelled():
        future.exception()


def _copy_future(source, destination):
    if destination.done():
        return
    if source.cancelled():
        destination.cancel()
    else:
        error = source.exception()
        if error is not None:
            # asyncio deliberately forbids StopIteration as a Future failure
            # because it can silently end an awaiting coroutine (PEP 479).
            if isinstance(error, StopIteration):
                replacement = RuntimeError("concurrent operation raised StopIteration")
                replacement.__cause__ = error
                error = replacement
            destination.set_exception(error)
        else:
            destination.set_result(source.result())


class _FutureDelivery:
    def __init__(self, loop, destination):
        self.loop, self.destination = loop, destination

    def __call__(self, source):
        loop, destination = self.loop, self.destination
        self.loop = self.destination = None
        if not loop.is_closed():
            try:
                loop.call_soon_threadsafe(_copy_future, source, destination)
            except RuntimeError:
                if not loop.is_closed():
                    raise


def bridge_future(source):
    """Transfer outcomes without asyncio's concurrent-exception conversion."""
    loop = asyncio.get_running_loop()
    destination = loop.create_future()
    reference = weakref.ref(source)

    def cancelled(done):
        original = reference()
        if done.cancelled() and original is not None:
            original.cancel()

    destination.add_done_callback(cancelled)
    source.add_done_callback(_FutureDelivery(loop, destination))
    return destination
