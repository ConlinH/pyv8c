#include "bridge_base.h"
#include "datetime.h"
#include "entry.h"
#include "runtime.h"

#include "context.h"
#include "isolate.h"
#include "microtask_queue.h"
#include "foreground_notification.h"

using namespace v8;

static PyObject* isolate_new(PyTypeObject* type, PyObject* args, PyObject* kwargs);
static void isolate_dealloc(isolate_c* self);
static PyObject* isolate_close(isolate_c* self, PyObject* args);
static PyObject* isolate_context(isolate_c* self, PyObject* args, PyObject* kwargs);
static PyObject* isolate_gc(isolate_c* self, PyObject* args);
static PyObject* isolate_set_host_datetime(isolate_c* self, PyObject* args, PyObject* kwargs);
static PyObject* isolate_get_closed(isolate_c* self, void* closure);
static PyObject* isolate_get_external_memory(isolate_c* self, void* closure);
static PyObject* isolate_get_live_v8_handles(isolate_c* self, void* closure);
static PyObject* isolate_foreground_version(isolate_c* self, PyObject*);
static PyObject* isolate_foreground_wait(isolate_c* self, PyObject*);
static PyObject* isolate_foreground_deadline(isolate_c* self, PyObject*);
static PyObject* isolate_set_wasm_response_handler(isolate_c* self, PyObject*);

static PyMethodDef isolate_methods[] = {
    {"close", (PyCFunction)PythonBoundary<isolate_close>::call, METH_NOARGS, "close this V8 isolate"},
    {"context", (PyCFunction)PythonBoundary<isolate_context>::call, METH_VARARGS | METH_KEYWORDS,
     "create a Context owned by this isolate"},
    {"gc", (PyCFunction)PythonBoundary<isolate_gc>::call, METH_NOARGS, "request a full V8 garbage collection"},
    {"set_host_datetime", (PyCFunction)PythonBoundary<isolate_set_host_datetime>::call,
     METH_VARARGS | METH_KEYWORDS, "set this isolate's IANA time zone and BCP 47 language"},
    {"_foreground_version", (PyCFunction)PythonBoundary<isolate_foreground_version>::call, METH_NOARGS, nullptr},
    {"_foreground_wait", (PyCFunction)PythonBoundary<isolate_foreground_wait>::call, METH_O, nullptr},
    {"_foreground_deadline", (PyCFunction)PythonBoundary<isolate_foreground_deadline>::call, METH_NOARGS, nullptr},
    {"_set_wasm_response_handler", (PyCFunction)PythonBoundary<isolate_set_wasm_response_handler>::call, METH_O, nullptr},
    {nullptr},
};

static PyObject* isolate_set_wasm_response_handler(isolate_c* self, PyObject* handler) {
    auto* state = isolate_state_from_python((PyObject*)self);
    if (!state || !state->set_wasm_response_handler(handler)) return nullptr;
    Py_RETURN_NONE;
}

static PyObject* isolate_foreground_version(isolate_c* self, PyObject*) {
    if (!isolate_state_from_python((PyObject*)self)) return nullptr;
    auto notifications = runtime_manager.foreground_notifications();
    if (!notifications) { PyErr_SetString(PyExc_RuntimeError, "V8 platform is closed"); return nullptr; }
    return PyLong_FromUnsignedLongLong(notifications->version());
}

static PyObject* isolate_foreground_wait(isolate_c* self, PyObject* arg) {
    if (!isolate_state_from_python((PyObject*)self)) return nullptr;
    auto version = PyLong_AsUnsignedLongLong(arg);
    if (PyErr_Occurred()) return nullptr;
    auto notifications = runtime_manager.foreground_notifications();
    if (!notifications) { PyErr_SetString(PyExc_RuntimeError, "V8 platform is closed"); return nullptr; }
    auto next = without_gil([&] { return notifications->wait(version); });
    return PyLong_FromUnsignedLongLong(next);
}

static PyObject* isolate_foreground_deadline(isolate_c* self, PyObject*) {
    auto* state = isolate_state_from_python((PyObject*)self);
    if (!state) return nullptr;
    auto notifications = runtime_manager.foreground_notifications();
    if (!notifications) { PyErr_SetString(PyExc_RuntimeError, "V8 platform is closed"); return nullptr; }
    auto token = runtime_manager.foreground_token(state->isolate());
    ForegroundNotifications::Clock::duration remaining;
    if (!notifications->next_deadline(token, remaining)) Py_RETURN_NONE;
    return PyFloat_FromDouble(std::chrono::duration<double>(remaining).count());
}

static PyGetSetDef isolate_getset[] = {
    {(char*)"closed", (getter)PythonBoundary<isolate_get_closed>::call, nullptr, nullptr, nullptr},
    {(char*)"external_memory",
     (getter)PythonBoundary<isolate_get_external_memory>::call, nullptr,
     (char*)"bytes this isolate has reported to V8 as kept alive outside its heap",
     nullptr},
    {(char*)"live_v8_handles",
     (getter)PythonBoundary<isolate_get_live_v8_handles>::call, nullptr,
     (char*)"persistent handles this isolate is still holding; closing it is refused while any are live",
     nullptr},
    {nullptr},
};

static PyType_Slot isolate_slots[] = {
    {Py_tp_new, (void*)PythonBoundary<isolate_new>::call},
    {Py_tp_dealloc, (void*)PythonBoundary<isolate_dealloc>::call},
    {Py_tp_methods, isolate_methods},
    {Py_tp_getset, isolate_getset},
    {Py_tp_doc, (void*)"independent V8 isolate"},
    {0, nullptr},
};

static PyType_Spec isolate_spec = {
    "pyv8c.pyv8.Isolate",
    sizeof(isolate_c),
    0,
    Py_TPFLAGS_DEFAULT | Py_TPFLAGS_BASETYPE,
    isolate_slots,
};

PyTypeObject* isolate_type = nullptr;

int
isolate_type_init()
{
    isolate_type = (PyTypeObject*)PyType_FromSpec(&isolate_spec);
    return isolate_type == nullptr ? -1 : 0;
}

IsolateState*
isolate_state_from_python(PyObject* object, bool allow_none)
{
    if (allow_none && (object == nullptr || object == Py_None))
        return select_isolate(IsolateSelection::DefaultOnly);
    if (object == nullptr || !PyObject_TypeCheck(object, isolate_type))
    {
        PyErr_SetString(PyExc_TypeError, "isolate must be an Isolate instance");
        return nullptr;
    }
    isolate_c* py_isolate = (isolate_c*)object;
    if (py_isolate->state == nullptr || !py_isolate->state->is_active())
    {
        PyErr_SetString(PyExc_RuntimeError, "V8 isolate has been closed");
        return nullptr;
    }
    return py_isolate->state;
}

static PyObject*
isolate_new(PyTypeObject* type, PyObject* args, PyObject* kwargs)
{
    PyObject* thread_bound = Py_False;
    static const char* keywords[] = {"thread_bound", nullptr};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "|$O", (char**)keywords, &thread_bound))
        return nullptr;
    if (!PyBool_Check(thread_bound)) {
        PyErr_SetString(PyExc_TypeError, "thread_bound must be a bool");
        return nullptr;
    }

    IsolateState* state = runtime_manager.create_isolate(thread_bound == Py_True);
    if (state == nullptr)
        return nullptr;

    isolate_c* self = (isolate_c*)PyType_GenericAlloc(type, 0);
    if (self == nullptr)
    {
        runtime_manager.destroy_isolate(state, false);
        return nullptr;
    }
    self->state = state;
    state->bind_python_owner((PyObject*)self);
    return (PyObject*)self;
}

static void
isolate_dealloc(isolate_c* self)
{
    if (self->state != nullptr)
    {
        self->state->bind_python_owner(nullptr);
        runtime_manager.destroy_isolate(self->state, false);
        self->state = nullptr;
        PyErr_Clear();
    }
    TP_FREE_ABI3(isolate_type, self);
}

static PyObject*
isolate_close(isolate_c* self, PyObject* args)
{
    if (self->state == nullptr)
        PYV8_RETURN_FALSE;
    if (!runtime_manager.destroy_isolate(self->state, true))
        return nullptr;
    self->state = nullptr;
    PYV8_RETURN_TRUE;
}

static PyObject*
isolate_context(isolate_c* self, PyObject* args, PyObject* kwargs)
{
    if (isolate_state_from_python((PyObject*)self) == nullptr)
        return nullptr;

    PyObject* call_kwargs = kwargs == nullptr ? PyDict_New() : PyDict_Copy(kwargs);
    if (call_kwargs == nullptr)
        return nullptr;
    if (PyDict_SetItemString(call_kwargs, "isolate", (PyObject*)self) < 0)
    {
        Py_DECREF((PyObject*)call_kwargs);
        return nullptr;
    }
    PyObject* result = PyObject_Call((PyObject*)context_type, args, call_kwargs);
    Py_DECREF((PyObject*)call_kwargs);
    return result;
}

static PyObject*
isolate_gc(isolate_c* self, PyObject* args)
{
    IsolateState* state = isolate_state_from_python((PyObject*)self);
    if (state == nullptr)
        return nullptr;
    IN_ISOLATE(state);
    isolate->LowMemoryNotification();
    PYV8_RETURN_NONE;
}

static PyObject*
isolate_set_host_datetime(isolate_c* self, PyObject* args, PyObject* kwargs)
{
    const char* timezone = nullptr;
    const char* locale = nullptr;
    static const char* keywords[] = {"timezone", "locale", nullptr};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "ss", (char**)keywords, &timezone, &locale))
        return nullptr;
    IsolateState* state = isolate_state_from_python((PyObject*)self);
    if (state == nullptr)
        return nullptr;

    char canonical_zone[128];
    char canonical_locale[128];
    if (pyv8_icu_canonicalize_timezone(timezone, canonical_zone, static_cast<int>(sizeof(canonical_zone))) != 0) {
        PyErr_Format(PyExc_ValueError, "unknown time zone: %s", timezone);
        return nullptr;
    }
    if (pyv8_icu_canonicalize_locale(locale, canonical_locale, static_cast<int>(sizeof(canonical_locale))) != 0) {
        PyErr_Format(PyExc_ValueError, "invalid language tag: %s", locale);
        return nullptr;
    }
    const bool configured = state->host_datetime_configured();
    if (configured && state->host_timezone() == canonical_zone && state->host_locale() == canonical_locale)
        return Py_BuildValue("ss", canonical_zone, canonical_locale);
    const bool locale_changed = !configured || state->host_locale() != canonical_locale;

    IN_ISOLATE(state);
    state->set_host_datetime(canonical_zone, canonical_locale);
    pyv8_apply_thread_datetime(state);
    if (locale_changed)
        isolate->LocaleConfigurationChangeNotification();
    // kRedetect would adopt the host zone and erase the override just applied.
    isolate->DateTimeConfigurationChangeNotification(Isolate::TimeZoneDetection::kSkip);
    return Py_BuildValue("ss", canonical_zone, canonical_locale);
}

static PyObject*
isolate_get_closed(isolate_c* self, void* closure)
{
    return PyBool_FromLong(self->state == nullptr || !self->state->is_active());
}

// Bytes this isolate has reported to V8 as kept alive outside its heap.
static PyObject*
isolate_get_external_memory(isolate_c* self, void* closure)
{
    const int64_t bytes = self->state == nullptr ? 0 : self->state->external_memory();
    return PyLong_FromLongLong((long long)bytes);
}

static PyObject*
isolate_get_live_v8_handles(isolate_c* self, void* closure)
{
    const std::size_t live = self->state == nullptr ? 0 : self->state->live_v8_handles();
    return PyLong_FromSize_t(live);
}

int isolate_register_microtask_queue(PyObject* module) {
    if (microtask_queue_type_init() < 0) return -1;
    return PyModule_AddObject(module, "MicrotaskQueue", (PyObject*)microtask_queue_type);
}
