"""The clock performance entries are timed with: an epoch origin, and offsets from it."""

import time


class Clock:
    """One document's timeline: an epoch a page can compare against ``Date.now()``,
    and the monotonic reading the entry offsets count from.  A navigation resets both."""

    #: Chrome reports time in whole 100-microsecond steps, and a page reads the
    #: difference: a finer clock is a fingerprint of its own.
    RESOLUTION_MS = 0.1

    def __init__(self):
        self.resolution_ms = self.RESOLUTION_MS
        self._epoch = time.time() * 1000.0
        self._monotonic = time.perf_counter()

    def time_origin(self):
        """The epoch milliseconds ``performance.timeOrigin`` reports."""
        return self._epoch

    def now_ms(self):
        """Milliseconds since :meth:`time_origin`, at the resolution the platform has."""
        return self.at(time.perf_counter())

    def at(self, monotonic):
        """Convert a shared rendering instant to this document's timeline."""
        elapsed = (monotonic - self._monotonic) * 1000.0
        # Rounding is monotonic, so readings still never run backwards.
        return round(elapsed / self.resolution_ms) * self.resolution_ms

    def reset(self, epoch_ms=None, *, monotonic=None):
        """Start a new timeline, as a document's navigation does.

        *monotonic* is the instant the timeline begins, for a document whose request
        was issued before it was committed: the epoch is shifted by the same amount, so
        offsets from it still line up with ``Date.now()``.
        """
        now = time.perf_counter()
        self._monotonic = now if monotonic is None else float(monotonic)
        self._epoch = (time.time() * 1000.0 - (now - self._monotonic) * 1000.0
                       if epoch_ms is None else float(epoch_ms))


def clock_of(services):
    """The clock of the document *services* belongs to, or a shared fallback."""
    realm = getattr(services, "realm_services", services)
    performance = getattr(realm, "performance", None)
    clock = getattr(performance, "clock", None)
    return clock if clock is not None else FALLBACK


#: For callers constructed without services.  Nobody resets it, so readings from it
#: stay consistent -- the behaviour the process-wide clock used to have.
FALLBACK = Clock()
