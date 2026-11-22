"""Owner-thread isolates preserve V8 thread state without allowing migration."""

import subprocess
import sys

import pytest

from pyv8c import pyv8


@pytest.mark.parametrize("value", [None, 1, "yes"])
def test_thread_binding_requires_a_boolean(value):
    with pytest.raises(TypeError, match="thread_bound must be a bool"):
        pyv8.Isolate(thread_bound=value)


def test_thread_binding_is_keyword_only():
    with pytest.raises(TypeError):
        pyv8.Isolate(True)


@pytest.mark.parametrize("owner_stopped", [False, True])
def test_foreign_final_reference_does_not_leak_the_isolate(owner_stopped):
    program = """
from concurrent.futures import ThreadPoolExecutor
from pyv8c import pyv8

def create():
    isolate = pyv8.Isolate(thread_bound=True)
    class Host: pass
    context = isolate.context(Host)
    assert context.exec_js("21 * 2") == 42
    context.close()
    return isolate

with ThreadPoolExecutor(max_workers=1) as executor:
    isolate = executor.submit(create).result()
    if not OWNER_STOPPED:
        del isolate
if OWNER_STOPPED:
    del isolate
assert pyv8.shutdown_v8()
""".replace("OWNER_STOPPED", repr(owner_stopped))
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_bound_isolate_keeps_contexts_and_callbacks_on_the_owner():
    class Host:
        pass

    isolate = pyv8.Isolate(thread_bound=True)
    first, second = isolate.context(Host), isolate.context(Host)
    try:
        first.expose(probe=lambda: pyv8.current_context(isolate) is first)
        second.expose(probe=lambda: pyv8.incumbent_context(isolate) is second)
        first.exec_js("var count=0")
        script = pyv8.Script(first, "++count")
        for expected in range(1, 21):
            assert first.exec_js(script) == expected
            assert first.exec_js("probe()") is True
            assert second.exec_js("probe()") is True
        assert pyv8.current_context(isolate) is None
    finally:
        first.close()
        second.close()
        assert isolate.close()


@pytest.mark.parametrize(
    "operation",
    ["execute", "context", "collect", "close_context", "close_isolate", "get_value", "call"],
)
def test_foreign_vm_operations_are_rejected_before_entering_v8(operation):
    expressions = {
        "execute": "context.exec_js('42')",
        "context": "isolate.context(Host)",
        "collect": "isolate.gc()",
        "close_context": "context.close()",
        "close_isolate": "isolate.close()",
        "get_value": "value['answer']",
        "call": "function()",
    }
    program = """
import threading
from pyv8c import pyv8
class Host: pass
isolate=pyv8.Isolate(thread_bound=True)
context=isolate.context(Host)
value=context.exec_js('({answer:42})')
function=context.exec_js('(()=>42)')
errors=[]
def run():
    try: OPERATION
    except RuntimeError as error: errors.append(str(error))
thread=threading.Thread(target=run)
try:
    thread.start();thread.join(5)
    assert not thread.is_alive()
    assert len(errors)==1 and 'owner thread' in errors[0],errors
    assert context.exec_js('21*2')==42 and function()==42
finally:
    context.close();isolate.close()
""".replace("OPERATION", expressions[operation])
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_foreign_gc_defers_context_cleanup_until_the_owner_turn():
    program = """
import gc,threading,weakref
from pyv8c import pyv8
class Host: pass
isolate=pyv8.Isolate(thread_bound=True)
host=Host();reference=weakref.ref(host)
context=isolate.context(host)
holder=[context]
del host,context
def collect():
    holder.clear()
    gc.collect()
thread=threading.Thread(target=collect)
try:
    thread.start();thread.join(5)
    assert not thread.is_alive()
    # Pending cleanup owns the Context until an owner entry can safely retire it.
    isolate.gc()
    gc.collect()
    assert reference() is None
    assert isolate.live_v8_handles==0
finally:isolate.close()
"""
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("bound_outer", [False, True])
def test_bound_and_migratable_isolates_can_nest_on_the_same_thread(bound_outer):
    class Host:
        pass

    a = pyv8.Isolate(thread_bound=bound_outer)
    b = pyv8.Isolate(thread_bound=not bound_outer)
    outer, inner = a.context(Host), b.context(Host)
    observed = []

    def back():
        observed.append((pyv8.current_context(a) is outer, pyv8.current_context(b) is inner))
        return outer.exec_js("21")

    try:
        inner.expose(back=back)
        outer.expose(nested=lambda: inner.exec_js("back()*2"))
        assert outer.exec_js("nested()") == 42
        assert observed == [(True, True)]
        assert outer.exec_js("40+2") == 42 and inner.exec_js("40+2") == 42
    finally:
        outer.close()
        inner.close()
        a.close()
        b.close()


def test_foreign_gc_retires_value_handles_on_the_owner():
    import gc
    import threading

    class Host:
        pass

    isolate = pyv8.Isolate(thread_bound=True)
    context = isolate.context(Host)
    try:
        baseline = isolate.live_v8_handles
        value = context.exec_js("({answer:42})")
        assert isolate.live_v8_handles > baseline
        holder = [value]
        del value

        def collect():
            holder.clear()
            gc.collect()

        thread = threading.Thread(target=collect)
        thread.start()
        thread.join(5)
        assert not thread.is_alive()
        assert context.exec_js("42") == 42
        gc.collect()
        assert isolate.live_v8_handles == baseline
    finally:
        context.close()
        isolate.close()


def test_bound_execution_timeout_preserves_recovery():
    program = """
from pyv8c import pyv8
class Host: pass
isolate=pyv8.Isolate(thread_bound=True)
context=isolate.context(Host,timeout=.02)
try:
    try: context.exec_js('for(;;){}')
    except pyv8.JavaScriptTerminated: pass
    else: raise AssertionError('execution was not terminated')
    assert context.exec_js('21*2')==42
finally:context.close();isolate.close()
"""
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_bound_operation_can_be_stopped_from_a_control_thread():
    program = """
import threading
from pyv8c import pyv8
class Host: pass
entered=threading.Event()
published={};results=[];errors=[]
def run():
    isolate=pyv8.Isolate(thread_bound=True)
    context=isolate.context(Host)
    ticket=context.begin_operation(0)
    published.update(context=context,ticket=ticket)
    context.expose(mark=lambda:entered.set())
    try:
        try: context.exec_js('mark();for(;;){}')
        except pyv8.JavaScriptTerminated: results.append('stopped')
        else: errors.append('execution was not stopped')
        finally:context.end_operation(ticket)
        results.append(context.exec_js('21*2'))
    except BaseException as error:errors.append(error)
    finally:context.close();isolate.close()
thread=threading.Thread(target=run,daemon=True)
thread.start()
assert entered.wait(3)
assert published['context'].terminate_operation(published['ticket'])
thread.join(5)
assert not thread.is_alive() and not errors,errors
assert results==['stopped',42],results
"""
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stdout + result.stderr
