"""Ownership release must not depend on transport terminal ordering."""
import pytest

from pyv8c.foundation.lifecycle import ResourceScope
from pyv8c.foundation.load_handle import LoadHandle
from pyv8c.platform.resources.ownership import ResourceOwnership


def test_early_delivery_pending_handle_and_duplicate_callback():
    owner = ResourceOwnership(ResourceScope())
    results = []
    def start(handle, url, request, callback):
        callback("ok", None)
        callback("duplicate", None)
    handle = owner.issue(start, "", {}, lambda *args: results.append(args))
    assert results == [("ok", None)]
    assert not handle.done
    assert owner.active_count == 0


def test_close_during_start_cancels_and_drops_result():
    scope = ResourceScope()
    owner = ResourceOwnership(scope)
    results = []
    def start(handle, url, request, callback):
        scope.close()
        assert handle.cancelled
        callback("late", None)
    handle = owner.issue(start, "", {}, lambda *args: results.append(args))
    assert handle.cancelled
    assert not results
    assert owner.active_count == 0


def test_finish_before_delivery_still_delivers_once():
    owner = ResourceOwnership(ResourceScope())
    callbacks = []
    results = []
    handle = owner.issue(lambda handle, url, request, cb: callbacks.append(cb), "", {},
                         lambda *args: results.append(args))
    handle.finish()
    assert owner.active_count == 0
    callbacks[0]("ok", None)
    callbacks[0]("again", None)
    assert results == [("ok", None)]


def test_callback_exception_releases_ticket():
    owner = ResourceOwnership(ResourceScope())
    def start(handle, url, request, callback):
        callback(None, None)
    def fail(*args):
        raise ValueError("user callback")
    with pytest.raises(ValueError, match="user callback"):
        owner.issue(start, "", {}, fail)
    assert owner.active_count == 0


def test_cancel_exception_does_not_strand_other_tickets():
    owner = ResourceOwnership(ResourceScope())
    broken = LoadHandle()
    class Transport:
        def cancel(self):
            raise ValueError("transport")
    broken.bind_transport(Transport())
    other = LoadHandle()
    owner.track(broken)
    owner.track(other)
    with pytest.raises(ValueError, match="transport"):
        owner.close()
    assert broken.cancelled and other.cancelled
    assert owner.active_count == 0
