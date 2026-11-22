"""Context observation must not enter an idle isolate or wait for its owner."""

import subprocess
import sys

import pytest

from pyv8c import pyv8


@pytest.mark.parametrize("query", ["current_context", "incumbent_context"])
@pytest.mark.parametrize("explicit", [False, True])
def test_query_on_another_thread_does_not_wait_for_the_active_context(query, explicit):
    program = f"""
import threading
from pyv8c import pyv8
class Window: pass
explicit = {explicit!r}
isolate = pyv8.Isolate() if explicit else None
context = pyv8.Context(Window, **({{'isolate': isolate}} if explicit else {{}}))
entered, release = threading.Event(), threading.Event()
released, results, errors = [], [], []
def pause():
    entered.set()
    released.append(release.wait(3))
    return 42
def run():
    try: results.append(context.exec_js('pause()'))
    except BaseException as error: errors.append(error)
context.expose(pause=pause)
thread = threading.Thread(target=run)
try:
    thread.start()
    assert entered.wait(3)
    try:
        assert pyv8.{query}(*([isolate] if explicit else [])) is None
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert not errors, errors
    assert results == [42] and released == [True], (results, released)
finally:
    release.set()
    thread.join(5)
    context.close()
    if isolate is not None: isolate.close()
"""
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("query", ["current_context", "incumbent_context"])
def test_nested_query_preserves_outer_and_inner_isolate_contexts(query):
    class Window:
        pass

    a, b, idle = pyv8.Isolate(), pyv8.Isolate(), pyv8.Isolate()
    outer, inner = a.context(Window), b.context(Window)
    observe = getattr(pyv8, query)
    observations = []

    def inspect():
        observations.append(
            (observe(a) is outer, observe(b) is inner, observe() is inner, observe(idle))
        )
        return 42

    try:
        inner.expose(inspect=inspect)
        outer.expose(nested=lambda: inner.exec_js("inspect()"))
        assert outer.exec_js("nested()") == 42
        assert observations == [(True, True, True, None)]
        assert observe(a) is None and observe(b) is None
    finally:
        outer.close()
        inner.close()
        a.close()
        b.close()
        idle.close()


@pytest.mark.parametrize("query", ["current_context", "incumbent_context"])
def test_idle_query_still_validates_the_explicit_isolate(query):
    observe = getattr(pyv8, query)
    with pytest.raises(TypeError):
        observe(object())
    isolate = pyv8.Isolate()
    isolate.close()
    with pytest.raises(RuntimeError, match="closed"):
        observe(isolate)
