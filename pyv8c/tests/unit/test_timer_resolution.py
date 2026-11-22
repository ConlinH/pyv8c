"""The platform timer the realm raises while it waits towards a deadline.

The request is process-global state, so the only thing that matters here is that
it is balanced: a leaked hold would keep every other process-wide timer at
millisecond resolution for the rest of the run.
"""

import sys

import pytest

from pyv8c.foundation import timer_resolution


def test_hold_is_usable_and_balanced():
    """Nesting holds leaves nothing behind, on every platform."""
    with timer_resolution.hold():
        with timer_resolution.hold():
            timer_resolution.acquire()
            timer_resolution.release()
    assert timer_resolution._holders == 0


def test_release_without_acquire_is_ignored():
    """An unbalanced release cannot drive the count below zero."""
    timer_resolution.release()
    assert timer_resolution._holders == 0


@pytest.mark.skipif(sys.platform != "win32", reason="only Windows has a coarse tick")
def test_holds_are_counted():
    """Concurrent realms each hold their own request."""
    with timer_resolution.hold():
        assert timer_resolution._holders == 1
        with timer_resolution.hold():
            assert timer_resolution._holders == 2
        assert timer_resolution._holders == 1
    assert timer_resolution._holders == 0
