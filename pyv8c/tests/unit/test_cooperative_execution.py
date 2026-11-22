"""Owner driver and Future transfer contracts, without requiring native V8."""

import asyncio
import threading
from concurrent.futures import Future

import pytest
from pyv8c.foundation.event_loop import run
from pyv8c.foundation.futures import bridge_future
from pyv8c.foundation.host_errors import HostStateError
from pyv8c.foundation.owner_executor import OwnerExecutor


@pytest.mark.parametrize("error", [TimeoutError("loader"), ValueError("bad value")])
def test_future_failures_keep_the_original_object(error):
    async def scenario():
        source = Future()
        destination = bridge_future(source)
        await asyncio.to_thread(source.set_exception, error)
        with pytest.raises(type(error)) as caught:
            await destination
        assert caught.value is error

    run(scenario())


def test_stop_iteration_cannot_silently_end_a_host_coroutine():
    async def scenario():
        source = Future()
        error = StopIteration("unexpected end")
        source.set_exception(error)
        with pytest.raises(RuntimeError, match="StopIteration") as caught:
            await bridge_future(source)
        assert caught.value.__cause__ is error

    run(scenario())


@pytest.mark.parametrize("cancel_source", [True, False])
def test_future_cancellation_crosses_the_bridge(cancel_source):
    async def scenario():
        source = Future()
        destination = bridge_future(source)
        (source if cancel_source else destination).cancel()
        with pytest.raises(asyncio.CancelledError):
            await destination
        await asyncio.sleep(0)
        assert source.cancelled()

    run(scenario())


def test_driver_coalesces_owner_wakes_and_retires_its_scheduled_handle():
    async def scenario():
        retired = []
        driver = OwnerExecutor(asyncio.get_running_loop(), on_closed=retired.append)
        for _ in range(100):
            driver.wakeup()
        scheduled = driver._soon
        driver.wakeup()
        assert driver._soon is scheduled
        driver.close()
        driver.close()
        assert retired == [driver] and scheduled.cancelled()
        assert not driver.post(lambda: None)

    run(scenario())


def test_close_settles_a_foreign_command_that_has_not_started(monkeypatch):
    async def scenario():
        driver = OwnerExecutor(asyncio.get_running_loop())
        results = []
        published = threading.Event()
        original = driver.wakeup

        def wakeup():
            original()
            published.set()

        monkeypatch.setattr(driver, "wakeup", wakeup)

        def sender():
            try:
                driver.call(lambda: pytest.fail("retired command ran"), timeout=2)
            except RuntimeError as error:
                results.append(str(error))

        thread = threading.Thread(target=sender)
        thread.start()
        try:
            # Keep the owner occupied until publication; do not pump the loop.
            assert published.wait(1)
            driver.close()
            await asyncio.to_thread(thread.join, 2)
            assert not thread.is_alive() and results == ["realm executor is closed"]
        finally:
            driver.close()

    run(scenario())


def test_foreign_teardown_is_rejected_and_owner_teardown_remains_possible():
    async def scenario():
        driver = OwnerExecutor(asyncio.get_running_loop())
        try:
            with pytest.raises(HostStateError, match="owner thread"):
                await asyncio.to_thread(driver.close)
            assert not driver.closed
        finally:
            driver.close()

    run(scenario())


def test_failed_turn_rejects_and_releases_foreign_senders_before_disposal(monkeypatch):
    async def scenario():
        failed, disposed, errors = [], [], []
        driver = OwnerExecutor(asyncio.get_running_loop(), on_failed=failed.append,
                               on_closed=disposed.append)
        published = threading.Event()
        wakeup = driver.wakeup
        def notified():
            wakeup()
            published.set()
        def interrupt():
            raise SystemExit("stop")
        driver.post(interrupt, _task_lane=None)
        monkeypatch.setattr(driver, "wakeup", notified)
        def sender():
            try:
                driver.call(lambda: pytest.fail("retired work ran"), timeout=2)
            except RuntimeError as exc:
                errors.append(str(exc))
        thread = threading.Thread(target=sender)
        thread.start()
        try:
            assert published.wait(1)
            with pytest.raises(SystemExit):
                driver._turn()
            await asyncio.to_thread(thread.join, 1)
            assert not thread.is_alive() and errors == ["realm executor is closed"]
            assert failed == [driver] and not disposed
            assert driver.failed and not driver.closed
            with pytest.raises(RuntimeError, match="owner executor failed"):
                await asyncio.to_thread(driver.call, lambda: None)
        finally:
            driver.close()
            await asyncio.to_thread(thread.join, 1)
        assert disposed == [driver]
    run(scenario())


def test_failure_is_published_before_retired_payload_finalizers_run():
    async def scenario():
        failure, observed = [], []
        driver = OwnerExecutor(asyncio.get_running_loop(), on_failed=failure.append)
        class Payload:
            def __del__(self):
                observed.append(failure == [driver])
        def interrupt():
            raise SystemExit("stop")
        driver.post(interrupt)
        driver.post(lambda item: None, Payload())
        try:
            with pytest.raises(SystemExit):
                driver._turn()
            assert observed == [True]
        finally:
            driver.close()
    run(scenario())
