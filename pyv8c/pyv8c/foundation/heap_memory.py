"""Rate limited snapshots for Chromium's legacy performance.memory API."""
import math
import struct
import time


def _float32(value):
    return struct.unpack('f', struct.pack('f', value))[0]


def _buckets():
    size = _float32(10_000_000)
    scale = _float32(math.exp(math.log(400) / 100))
    power, granularity = 100_000_000, 100_000
    values = []
    for _ in range(100):
        value = int(size)
        values.append(value - value % granularity)
        size = _float32(size * scale)
        if size >= power:
            power *= 10
            granularity *= 10
    return tuple(values)


_BUCKETS = _buckets()


def quantize_memory_size(size):
    return next((bound for bound in _BUCKETS if size <= bound), _BUCKETS[-1])


class HeapMemoryCache:
    """One execution thread shares its snapshot across document realms."""

    def __init__(self):
        self._updated = None
        self._values = {}

    def snapshot(self, read, *, precise=False):
        now = time.monotonic()
        interval = 0.05 if precise else 1200
        if self._updated is None or now - self._updated >= interval:
            raw = read()
            external = raw.get('external', 0)
            values = {'used': raw.get('used', 0) + external,
                      'total': raw.get('physical', raw.get('total', 0)) + external,
                      'limit': raw.get('limit', 0)}
            self._values = values if precise else {
                key: quantize_memory_size(value) for key, value in values.items()}
            self._updated = now
        return dict(self._values)
