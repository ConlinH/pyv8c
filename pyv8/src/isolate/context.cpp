#include "bridge_base.h"
#include "wasm_streaming.h"
#include <cmath>
#include <Python.h>
#include "entry.h"
#include "execution.h"
#include "runtime.h"
#include "crossing.h"
#include <v8.h>
#include <v8-debug.h>
#include <algorithm>
#include <limits>

#include "context.h"
#include "debugger.h"
#include "error_hooks.h"
#include "idl_spec.h"
#include "script.h"
#include "convert.h"
#include "jsobject.h"
#include "jsvalue.h"
#include "pyclass.h"
#include "v8tools.h"
#include "isolate.h"
#include "watchdog.h"
#include "microtask_queue.h"

using namespace v8;

static std::size_t trace_next_utf8(const std::string& text, std::size_t index)
{
    if (index >= text.size()) return index;
    auto first = static_cast<unsigned char>(text[index]);
    std::size_t width = first < 0x80 ? 1 : first < 0xe0 ? 2 : first < 0xf0 ? 3 : 4;
    return std::min(index + width, text.size());
}

static bool trace_glob_matches(const std::string& pattern, const std::string& source)
{
    std::size_t p = 0, s = 0, star = std::string::npos, retry = 0;
    while (s < source.size()) {
        if (p < pattern.size() && pattern[p] == '?') {
            ++p;
            s = trace_next_utf8(source, s);
        } else if (p < pattern.size() && pattern[p] == source[s]) {
            ++p;
            ++s;
        } else if (p < pattern.size() && pattern[p] == '*') {
            star = p++;
            retry = s;
        } else if (star != std::string::npos) {
            p = star + 1;
            retry = trace_next_utf8(source, retry);
            s = retry;
        } else {
            return false;
        }
    }
    while (p < pattern.size() && pattern[p] == '*') ++p;
    return p == pattern.size();
}

bool context_trace_source_matches(context_c* context)
{
    auto* filter = context->trace_source_filter;
    if (filter == nullptr) return true;
    auto* isolate = v8::Isolate::GetCurrent();
    if (isolate == nullptr) return false;
    auto script = v8::StackTrace::CurrentScriptNameOrSourceURL(isolate);
    if (script.IsEmpty() || script->Length() == 0) return false;
    v8::String::Utf8Value name(isolate, script);
    if (*name == nullptr) return false;
    std::string source(*name, name.length());
    auto cached = filter->matches.find(source);
    if (cached != filter->matches.end()) return cached->second;
    bool match = false;
    for (const auto& pattern : filter->patterns) {
        if (trace_glob_matches(pattern, source)) {
            match = true;
            break;
        }
    }
    filter->matches.emplace(std::move(source), match);
    return match;
}

PyObject* context_promise_rejection_state(context_c* self, PyObject* args);
static PyObject* context_try_inspector(context_c* self, PyObject* args);
static PyObject* context_to_js(context_c* self, PyObject* value);
static PyObject* context_import_handle(context_c* self, PyObject* handle);
static PyObject* context_freeze(context_c* self, PyObject* value);
static PyObject* context_set_microtask_queue(context_c* self, PyObject* queue);
static PyObject* context_run_foreground_task(context_c*, PyObject*);
static PyObject* context_begin_operation(context_c*, PyObject*);
static PyObject* context_end_operation(context_c*, PyObject*);
static PyObject* context_terminate_operation(context_c*, PyObject*);
static PyObject* context_suspend_deadlines(context_c*, PyObject*);
static PyObject* context_resume_deadlines(context_c*, PyObject*);
static PyObject* context_set_deadline(context_c* self, PyObject* args, PyObject* kwargs);
static PyObject* context_clear_deadline(context_c* self, PyObject* ignored);
static PyObject* context_terminate(context_c* self, PyObject* ignored);
static int context_clear(context_c* self);
static int context_traverse(context_c* self, visitproc visit, void* arg);

static bool valid_timeout(double seconds) {
    const double maximum = std::chrono::duration<double>(
        std::chrono::steady_clock::time_point::max() - std::chrono::steady_clock::now()).count();
    if (!std::isfinite(seconds) || seconds > maximum) {
        PyErr_SetString(PyExc_ValueError, "timeout must be finite and fit the monotonic clock");
        return false;
    }
    return true;
}

bool context_is_active(context_c* self) {
    return self != nullptr && self->state == ContextState::Active && self->isolate_state != nullptr &&
        !self->js_context.IsEmpty();
}

context_c* context_from_v8(Local<Context> context) {
    if (context.IsEmpty() || context->GetNumberOfEmbedderDataFields() <= CONTEXT_OBJECT_SLOT)
        return nullptr;
    auto* lifetime = v8_compat::context_embedder_pointer<ContextLifetime>(context, CONTEXT_OBJECT_SLOT);
    return lifetime ? lifetime->owner : nullptr;
}

bool context_is_detached(Local<Context> context) {
    context_c* owner = context_from_v8(context);
    return owner != nullptr && owner->detached;
}

PyObject* context_detach(context_c* self, PyObject* ignored) {
    (void)ignored;
    if (self->resources && !self->resources->wasm_streams.empty()) {
        ExecutionScope execution(self);
        if (!execution) return nullptr;
        wasm_stream_cancel_context(self);
    }
    //: Left behind: still readable, but nothing new is built inside it.
    self->detached = true;
    // Nothing new is asked of the embedder for a realm that no longer has a document.
    Py_CLEAR(self->lazy_factory);
    PYV8_RETURN_NONE;
}

// Whether an intercepted access may proceed.  The accepting path costs a few
// reads and a state check, no allocation and no string.
bool context_check_callback(Local<Context> context) {
    // Context::New may call interceptors before embedder data is installed.
    if (!context.IsEmpty() &&
        (context->GetNumberOfEmbedderDataFields() <= CONTEXT_OBJECT_SLOT ||
         !context->GetEmbedderData(CONTEXT_OBJECT_SLOT)->IsExternal())) return true;
    auto* owner = context.IsEmpty() ? nullptr :
        context_from_v8(context);
    if (context_is_active(owner)) return true;
    auto* isolate = Isolate::GetCurrent();
    isolate->ThrowException(Exception::Error(V8String(isolate, "V8 context is closed")));
    return false;
}

void context_register_handle(context_c* self, PyObject* handle, int (*clear)(PyObject*)) {
    if (self->resources == nullptr) self->resources = new ContextResources();
    self->resources->handles[handle] = clear;
}

void context_unregister_handle(context_c* self, PyObject* handle) {
    if (self != nullptr && self->resources != nullptr) self->resources->handles.erase(handle);
}

bool context_retain_dependency(Local<Context> target, PyObject* source) {
    auto* owner = context_from_v8(target);
    if (source == nullptr || owner == nullptr || source == (PyObject*)owner) return true;
    auto* origin = (context_c*)source;
    // Already recorded?  The reverse index knows, so the list below does not have
    // to be searched to answer that: the list is there to hold the strong
    // references that keep the origins alive, and reading through it turned a
    // second and third window into a scan of the first.
    if (owner->resources != nullptr && owner->resources->dependencies.count(origin) != 0)
        return true;
    if (owner->dependencies == nullptr) owner->dependencies = PyList_New(0);
    if (owner->dependencies == nullptr) return false;
    try {
        if (origin->resources == nullptr) origin->resources = new ContextResources();
        if (owner->resources == nullptr) owner->resources = new ContextResources();
        origin->resources->dependents.insert(owner);
        owner->resources->dependencies.insert(origin);
        owner->resources->dependency_slots[origin] = PyList_Size(owner->dependencies);
    } catch (const std::bad_alloc&) {
        if (origin->resources) origin->resources->dependents.erase(owner);
        if (owner->resources) {
            owner->resources->dependencies.erase(origin);
            owner->resources->dependency_slots.erase(origin);
        }
        PyErr_NoMemory();
        return false;
    }
    if (PyList_Append(owner->dependencies, source) < 0) {
        origin->resources->dependents.erase(owner);
        owner->resources->dependencies.erase(origin);
        owner->resources->dependency_slots.erase(origin);
        return false;
    }
    return true;
}

// Drops *origin* from *owner*'s reference list, by swapping the last entry into
// its place: the list is the strong references, and the slot map says where the
// entry is, so neither lookup nor removal walks it.
void ContextResources::drop_dependency(context_c* owner, context_c* origin) {
    if (owner->dependencies == nullptr || owner->resources == nullptr)
        return;
    auto slot = owner->resources->dependency_slots.find(origin);
    if (slot == owner->resources->dependency_slots.end())
        return;
    const Py_ssize_t index = slot->second;
    const Py_ssize_t last = PyList_Size(owner->dependencies) - 1;
    owner->resources->dependency_slots.erase(slot);
    if (index < 0 || index > last)
        return;
    if (index != last) {
        PyObject* moved = PyList_GetItem(owner->dependencies, last);
        if (moved != nullptr) {
            Py_INCREF(moved);
            // Releasing the entry it replaces is the reference this drops.
            if (PyList_SetItem(owner->dependencies, index, moved) == 0)
                owner->resources->dependency_slots[(context_c*)moved] = index;
            else
                Py_DECREF(moved);
        }
    }
    PySequence_DelItem(owner->dependencies, last);
}

void ContextResources::unlink_dependencies(context_c* self) {
    if (self->resources != nullptr) {
        auto& dependents = self->resources->dependents;
        while (!dependents.empty()) {
            auto* target = *dependents.begin();
            dependents.erase(target);
            if (target->resources != nullptr) target->resources->dependencies.erase(self);
            if (target->resources) target->resources->drop_dependency(target, self);
        }
        for (auto* origin : self->resources->dependencies) {
            if (origin->resources != nullptr) origin->resources->dependents.erase(self);
        }
        self->resources->dependencies.clear();
        self->resources->dependency_slots.clear();
    }
    Py_CLEAR(self->dependencies);
}

static PyObject* context_heap_statistics(context_c* self, PyObject* args);

PyMethodDef context_methods[] = {
    {"_try_inspector", (PyCFunction)PythonBoundary<context_try_inspector>::call, METH_VARARGS, NULL},
    {"exec_js", (PyCFunction)PythonBoundary<context_run>::call, METH_VARARGS | METH_KEYWORDS, NULL},
    {"js_obj", (PyCFunction)PythonBoundary<context_js_obj>::call, METH_O, NULL},
    {"freeze", (PyCFunction)PythonBoundary<context_freeze>::call, METH_O,
     "convert a Python value to V8 and freeze it in this realm"},
    {"expose", (PyCFunction)PythonBoundary<context_expose>::call, METH_VARARGS | METH_KEYWORDS, NULL},
    {"hide_global", (PyCFunction)PythonBoundary<context_hide_global>::call, METH_O,
     "remove a name this realm exposed, as if it had never been installed"},
    {"define_global", (PyCFunction)PythonBoundary<context_define_global>::call, METH_VARARGS | METH_KEYWORDS,
     "install one global value with the property attributes the caller names"},
    {"install", (PyCFunction)PythonBoundary<context_install>::call, METH_O,
     "install each name as a non-enumerable lazy constructor"},
    {"install_ordered", (PyCFunction)PythonBoundary<context_install_ordered>::call, METH_O,
     "install global instance members and lazy constructors in the supplied order"},
    {"gc", (PyCFunction)PythonBoundary<context_gc>::call, METH_NOARGS, NULL},
    {"heap_statistics", (PyCFunction)PythonBoundary<context_heap_statistics>::call, METH_NOARGS,
     "bytes of this isolate's heap in use, committed, and its ceiling, as a dict"},
    {"enqueue_microtask", (PyCFunction)PythonBoundary<context_enqueue_microtask>::call, METH_O, NULL},
    {"set_microtask_queue", (PyCFunction)PythonBoundary<context_set_microtask_queue>::call, METH_O, NULL},
    {"set_code_generation_handler", (PyCFunction)PythonBoundary<context_set_code_generation_handler>::call,
     METH_O, "Set a realm policy for eval and Function source; None restores engine defaults."},
    {"_run_foreground_task", (PyCFunction)PythonBoundary<context_run_foreground_task>::call, METH_NOARGS,
     "run at most one ready V8 foreground task without waiting or a microtask checkpoint"},
    {"_wasm_stream_feed", (PyCFunction)PythonBoundary<wasm_stream_feed>::call, METH_VARARGS, NULL},
    {"_wasm_stream_finish", (PyCFunction)PythonBoundary<wasm_stream_finish>::call, METH_O, NULL},
    {"_wasm_stream_abort", (PyCFunction)PythonBoundary<wasm_stream_abort>::call, METH_VARARGS, NULL},
    {"begin_operation", (PyCFunction)PythonBoundary<context_begin_operation>::call, METH_O, NULL},
    {"end_operation", (PyCFunction)PythonBoundary<context_end_operation>::call, METH_O, NULL},
    {"terminate_operation", (PyCFunction)PythonBoundary<context_terminate_operation>::call, METH_O, NULL},
    {"_suspend_deadlines", (PyCFunction)PythonBoundary<context_suspend_deadlines>::call, METH_NOARGS, NULL},
    {"_resume_deadlines", (PyCFunction)PythonBoundary<context_resume_deadlines>::call, METH_O, NULL},
    {"_promise_rejection_state", (PyCFunction)PythonBoundary<context_promise_rejection_state>::call,
     METH_VARARGS, "private, non-executing promise identity/notification bookkeeping"},
    {"set_error_hooks", (PyCFunction)PythonBoundary<context_set_error_hooks>::call,
     METH_VARARGS | METH_KEYWORDS, NULL},
    {"close", (PyCFunction)PythonBoundary<context_close>::call, METH_NOARGS, NULL},
    {"detach", (PyCFunction)PythonBoundary<context_detach>::call, METH_NOARGS,
     "mark the realm as left behind by a navigation"},
    {"to_js", (PyCFunction)PythonBoundary<context_to_js>::call, METH_O, NULL},
    {"import_handle", (PyCFunction)PythonBoundary<context_import_handle>::call, METH_O, NULL},
    {"set_deadline", (PyCFunction)PythonBoundary<context_set_deadline>::call, METH_VARARGS | METH_KEYWORDS, NULL},
    {"clear_deadline", (PyCFunction)PythonBoundary<context_clear_deadline>::call, METH_NOARGS, NULL},
    {"terminate", (PyCFunction)PythonBoundary<context_terminate>::call, METH_NOARGS, NULL},
    {NULL},
};

static IsolateState*
context_require_isolate(context_c* self)
{
    if (!context_is_active(self) || !self->isolate_state->is_active())
    {
        PyErr_SetString(PyExc_RuntimeError, "V8 context is no longer active");
        return nullptr;
    }
    return self->isolate_state;
}
// Promise rejection hooks must not re-enter JavaScript. A private property on
// the actual promise provides identity and notification state without a strong
// promise registry, user accessors, microtasks or another execution crossing.
PyObject* context_promise_rejection_state(context_c* self, PyObject* args) {
    PyObject* object;
    int action;
    if (!PyArg_ParseTuple(args, "Oi", &object, &action)) return nullptr;
    if (action < 0 || action > 2) {
        PyErr_SetString(PyExc_ValueError, "invalid promise rejection state action");
        return nullptr;
    }
    if (!PyObject_TypeCheck(object, js_object_type)) {
        PyErr_SetString(PyExc_TypeError, "expected a JavaScript Promise");
        return nullptr;
    }
    auto* wrapper = (js_object*)object;
    ExecutionScope execution(self, object);
    if (!execution || !js_object_require_active(wrapper)) return nullptr;
    auto* isolate = execution.isolate();
    auto context = execution.context();
    if (wrapper->isolate_state != self->isolate_state) {
        PyErr_SetString(PyExc_ValueError, "promise belongs to another isolate");
        return nullptr;
    }
    auto value = wrapper->object.Get(isolate);
    if (!value->IsPromise()) {
        PyErr_SetString(PyExc_TypeError, "expected a JavaScript Promise");
        return nullptr;
    }
    auto promise = value.As<Promise>();
    Local<Context> origin;
    if (!promise->GetCreationContext().ToLocal(&origin) || context_from_v8(origin) != self) {
        PyErr_SetString(PyExc_ValueError, "promise belongs to another context");
        return nullptr;
    }
    auto failed = [&]() -> PyObject* {
        if (isolate->IsExecutionTerminating()) { Py_RETURN_NONE; }
        PyErr_SetString(PyExc_RuntimeError, "could not access promise rejection state");
        return nullptr;
    };
    // None is an interrupted bookkeeping operation, not a promise identity.
    if (isolate->IsExecutionTerminating()) { Py_RETURN_NONE; }
    if (!self->resources) self->resources = new ContextResources();
    auto* resources = self->resources;
    if (resources->rejection_key.IsEmpty())
        resources->rejection_key.Set(self->isolate_state, isolate, Private::New(isolate));
    auto key = resources->rejection_key.Get(isolate);
    Local<Value> stored;
    if (!promise->GetPrivate(context, key).ToLocal(&stored)) return failed();
    Local<Array> entry;
    if (stored->IsUndefined()) {
        if (resources->next_rejection_id == std::numeric_limits<uint64_t>::max()) {
            PyErr_SetString(PyExc_OverflowError, "promise rejection identities exhausted");
            return nullptr;
        }
        Local<Value> fields[] = {BigInt::NewFromUnsigned(isolate, ++resources->next_rejection_id), False(isolate)};
        entry = Array::New(isolate, fields, 2);
        if (!promise->SetPrivate(context, key, entry).FromMaybe(false)) return failed();
    } else {
        if (!stored->IsArray()) return failed();
        entry = stored.As<Array>();
    }
    if (action == 0) {
        Local<Value> identity;
        if (!entry->Get(context, 0).ToLocal(&identity)) return failed();
        return PyLong_FromUnsignedLongLong(identity.As<BigInt>()->Uint64Value());
    }
    if (action == 1) {
        if (promise->State() != Promise::kRejected) {
            PyErr_SetString(PyExc_ValueError, "only rejected promises may be notified");
            return nullptr;
        }
        if (!entry->Set(context, 1, True(isolate)).FromMaybe(false)) return failed();
        Py_RETURN_NONE;
    }
    Local<Value> notified;
    if (!entry->Get(context, 1).ToLocal(&notified)) return failed();
    if (!notified->IsTrue()) return PyTuple_Pack(2, Py_False, Py_None);
    if (!entry->Set(context, 1, False(isolate)).FromMaybe(false)) return failed();
    PyOwnedRef reason(py_from_js(promise->Result(), context));
    if (!reason.get()) return nullptr;
    return PyTuple_Pack(2, Py_True, reason.get());
}

static PyObject* context_try_inspector(context_c* self, PyObject* args) {
    PyObject *callback, *wake;
    if (!PyArg_ParseTuple(args, "OO", &callback, &wake)) return nullptr;
    if (!PyCallable_Check(callback) || !PyCallable_Check(wake)) {
        PyErr_SetString(PyExc_TypeError, "Inspector callbacks must be callable");
        return nullptr;
    }
    auto state = context_require_isolate(self);
    if (!state) return nullptr;
    IsolateLockScope scope(state, true);
    if (!scope.entered()) {
        if (!state->register_inspector_waiter(wake)) return nullptr;
        Py_RETURN_FALSE;
    }
    PyObject* result = PyObject_CallNoArgs(callback);
    if (!result) return nullptr;
    Py_DECREF((PyObject*)result);
    Py_RETURN_TRUE;
}
static PyObject* context_isolate_id(context_c* self, void*) {
    auto state = context_require_isolate(self);
    if (!state) return nullptr;
    return PyLong_FromVoidPtr(state->isolate());
}
static PyObject* context_get_detached(context_c* self, void*) {
    return PyBool_FromLong(self->detached ? 1 : 0);
}
// The Python wrappers this realm still owns; zero once it has released them.
static PyObject* context_live_handles(context_c* self, void*) {
    auto* resources = self->resources;
    return PyLong_FromSsize_t(resources == nullptr ? 0
                              : static_cast<Py_ssize_t>(resources->handle_count()));
}
// How many crossings stopped because the heap was full; the limit itself is
// recovered from: the next crossing resumes the isolate.
static PyObject* context_heap_terminations(context_c* self, void*) {
    auto state = context_require_isolate(self);
    if (!state) return nullptr;
    return PyLong_FromSize_t(state->watchdog().heap_terminations());
}
static PyObject* context_heap_statistics(context_c* self, PyObject* args) {
    auto state = context_require_isolate(self);
    if (!state) return nullptr;
    v8::HeapStatistics statistics;
    state->isolate()->GetHeapStatistics(&statistics);
    return Py_BuildValue("{s:L,s:L,s:L,s:L,s:L}",
                         "used", (long long)statistics.used_heap_size(),
                         "total", (long long)statistics.total_heap_size(),
                         "limit", (long long)statistics.heap_size_limit(),
                         "physical", (long long)statistics.total_physical_size(),
                         "external", (long long)statistics.external_memory());
}

PyGetSetDef context_getset[] = {
    {(char*)"isolate_id", (getter)PythonBoundary<context_isolate_id>::call, NULL, NULL, NULL},
    {(char*)"live_handles", (getter)PythonBoundary<context_live_handles>::call, NULL, NULL, NULL},
    {(char*)"heap_terminations", (getter)PythonBoundary<context_heap_terminations>::call, NULL, NULL, NULL},
    {(char*)"glob", (getter)PythonBoundary<context_get_global>::call, NULL, NULL, NULL},
    {(char*)"detached", (getter)PythonBoundary<context_get_detached>::call, NULL, NULL, NULL},
    {(char*)"trace", (getter)PythonBoundary<context_get_trace>::call, (setter)PythonBoundary<context_set_trace>::call, NULL, NULL},
    {(char*)"timeout", (getter)PythonBoundary<context_get_timeout>::call, (setter)PythonBoundary<context_set_timeout>::call, NULL, NULL},
    {NULL},
};
// We don't need mapping methods for ABI3 compatibility

// For ABI3 compatibility, we'll define the type using PyType_Spec
static PyType_Slot context_slots[] = {
    {Py_tp_traverse, (void*)PythonBoundary<context_traverse>::call},
    {Py_tp_clear, (void*)PythonBoundary<context_clear>::call},
    {Py_tp_dealloc, (void*)PythonBoundary<context_dealloc>::call},
    {Py_tp_new, (void*)PythonBoundary<context_new>::call},
    {Py_tp_methods, context_methods},
    {Py_tp_getattro, (void*)PythonBoundary<context_getattro>::call},
    {Py_tp_setattro, (void*)PythonBoundary<context_setattro>::call},
    {Py_tp_getset, context_getset},
    {Py_tp_doc, (void*)"v8 context"},
    // We can't use Py_tp_as_mapping in Limited API
    // {Py_tp_as_mapping, &context_mapping_for_spec},
    {0, NULL} };

static PyType_Spec context_spec = {
    "pyv8c.pyv8.Context",
    sizeof(context_c),
    0,
    Py_TPFLAGS_DEFAULT | Py_TPFLAGS_BASETYPE | Py_TPFLAGS_HAVE_GC,
    context_slots };

PyTypeObject* context_type = NULL;

int context_type_init()
{
    // Create the type using PyType_FromSpec for ABI3 compatibility
    context_type = (PyTypeObject *) PyType_FromSpec(&context_spec);
    if (context_type == NULL)
    {
        return -1;
    }
    // We don't want to DECREF here because we need to keep the type object alive
    return 0;
}


PyObject *
context_new(PyTypeObject *type, PyObject *args, PyObject *kwargs)
{
    if (!runtime_manager.ensure_initialized())
        return NULL;

    double timeout = 0;
    PyObject* py_timeout = Py_None;
    PyObject* global = NULL;
    PyObject* trace = Py_False;
    PyObject* ctx_type = NULL;
    PyObject* py_isolate = Py_None;
    PyObject* lazy_factory = Py_None;
    PyObject* trace_callback = Py_None;
    PyObject* traceable = Py_False;
    PyObject* reuse_global = Py_None;
    PyObject* py_microtasks = Py_None;
    PyObject* trace_scripts = Py_None;
    PyObject* ordered_global = Py_False;
    static const char *keywords[] = {"global_this", "trace", "ctx_type", "timeout", "isolate", "lazy_factory", "trace_callback", "traceable", "reuse_global", "microtask_queue", "trace_scripts", "ordered_global", NULL};

    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "O|OOOOOOOOOOO", (char **) keywords,
                                    &global, &trace, &ctx_type, &py_timeout, &py_isolate,
                                    &lazy_factory, &trace_callback, &traceable,
                                    &reuse_global, &py_microtasks, &trace_scripts,
                                    &ordered_global)) {
        return NULL;
    }

    if (ordered_global != Py_True && ordered_global != Py_False) {
        PyErr_SetString(PyExc_TypeError, "ordered_global must be a bool");
        return nullptr;
    }

    if (py_timeout != Py_None) timeout = PyFloat_AsDouble(py_timeout);
    if (PyErr_Occurred() || !valid_timeout(timeout)) return nullptr;

    if (lazy_factory != Py_None && !PyCallable_Check(lazy_factory)) {
        PyErr_SetString(PyExc_TypeError, "lazy_factory must be callable or None");
        return NULL;
    }

    if (trace_callback != Py_None && !PyCallable_Check(trace_callback)) {
        PyErr_SetString(PyExc_TypeError, "trace_callback must be callable or None");
        return nullptr;
    }
    std::vector<std::string> trace_patterns;
    if (trace_scripts != Py_None) {
        if (!PyTuple_Check(trace_scripts) || PyTuple_Size(trace_scripts) == 0) {
            PyErr_SetString(PyExc_ValueError, "trace_scripts must be a non-empty tuple of strings or None");
            return nullptr;
        }
        for (Py_ssize_t index = 0; index < PyTuple_Size(trace_scripts); ++index) {
            PyObject* pattern = PyTuple_GetItem(trace_scripts, index);
            if (!PyUnicode_Check(pattern) || PyUnicode_GetLength(pattern) == 0) {
                PyErr_SetString(PyExc_ValueError, "trace_scripts must contain non-empty strings");
                return nullptr;
            }
            Py_ssize_t size = 0;
            const char* text = PyUnicode_AsUTF8AndSize(pattern, &size);
            if (text == nullptr) return nullptr;
            trace_patterns.emplace_back(text, static_cast<std::size_t>(size));
        }
    }
    IsolateState* isolate_state = isolate_state_from_python(py_isolate, true);
    if (isolate_state == nullptr)
        return NULL;
    auto* microtasks = microtask_queue_get(py_microtasks, isolate_state);
    if (PyErr_Occurred()) return nullptr;
    IN_ISOLATE(isolate_state);

    // The frame's window again, which V8 accepts from the same template.
    Local<Value> reused;
    if (reuse_global != Py_None) {
        if (!PyObject_TypeCheck(reuse_global, js_object_type)) {
            PyErr_SetString(PyExc_TypeError, "reuse_global expects a realm's global object");
            return NULL;
        }
        js_object* reused_handle = (js_object*)reuse_global;
        if (reused_handle->isolate_state != isolate_state) {
            PyErr_SetString(PyExc_RuntimeError, "reuse_global belongs to another isolate");
            return NULL;
        }
        if (!js_object_require_active(reused_handle))
            return NULL;
        Local<Object> reused_object = reused_handle->object.Get(isolate);
        if (reused_object.IsEmpty() ||
            !isolate_state->transfer_python_object(reused_object)) {
            return NULL;
        }
        reused = reused_object;
    }

    if (global != NULL) {
        if (PyType_Check(global)) {
            PyObject* no_args = PyTuple_New(0);
            PyErr_PROPAGATE(no_args);
            global = PyObject_Call(global, no_args, NULL);
            Py_XDECREF((PyObject*)no_args);
            PyErr_PROPAGATE(global);
        }
        else {
            Py_INCREF((PyObject*)global);
        }
    }

    context_c* self = (context_c*)PyType_GenericAlloc(type, 0);
    if (self == NULL) {
        Py_XDECREF((PyObject*)global);
        return NULL;
    }
    self->isolate_state = isolate_state;
    self->py_isolate = nullptr;
    if (py_isolate != Py_None) {
        Py_INCREF((PyObject*)py_isolate);
        self->py_isolate = py_isolate;
    }
    self->lazy_factory = NULL;
    self->global_template = NULL;
    self->py_global = NULL;
    self->registry = PyDict_New();
    self->function_template_cache = PyDict_New();
    if (lazy_factory != Py_None) {
        Py_INCREF((PyObject*)lazy_factory);
        self->lazy_factory = lazy_factory;
    }
    self->py_global = global;
    self->has_done = false;
    self->ordered_global = ordered_global == Py_True;
    self->detached = false;
    self->timeout = timeout;
    self->trace = trace == Py_True;
    self->traceable = self->trace || traceable == Py_True;
    self->trace_depth = 0;
    self->trace_source_filter = trace_scripts == Py_None ? nullptr : new TraceSourceFilter();
    if (self->trace_source_filter != nullptr)
        self->trace_source_filter->patterns = std::move(trace_patterns);
    if (trace_callback != Py_None) {
        Py_INCREF((PyObject*)trace_callback);
        self->trace_callback = trace_callback;
    } else if (self->traceable) {
        PyObject* module = PyImport_ImportModule("pyv8c.pyv8.trace");
        if (module != nullptr) {
            self->trace_callback = PyObject_GetAttrString(module, "log_trace");
            Py_DECREF((PyObject*)module);
        }
        if (self->trace_callback == nullptr) {
            Py_DECREF((PyObject*)self);
            return nullptr;
        }
    }

    if (ctx_type == NULL) {
        ctx_type = PyUnicode_FromString("Top");
    } else {
        Py_INCREF((PyObject*)ctx_type);
    }
    if (ctx_type == NULL || self->registry == NULL || self->function_template_cache == NULL) {
        Py_XDECREF((PyObject*)ctx_type);
        Py_DECREF((PyObject*)self);
        return NULL;
    }

    if (self->trace_callback != nullptr) {
        // Event names indexed by the kind passed to TraceEvent::emit: 0 get,
        // 1 set, 2 method, 3 construct, 4 has, 5 descriptor.
        self->trace_event_names = Py_BuildValue(
            "(ssssss)", "get", "set", "method", "construct", "has", "descriptor");
        self->trace_context_type = ctx_type;
        Py_INCREF((PyObject*)ctx_type);
        if (self->trace_event_names == nullptr) {
            Py_DECREF((PyObject*)ctx_type);
            Py_DECREF((PyObject*)self);
            return nullptr;
        }
    }
    MaybeLocal<ObjectTemplate> global_template;
    if (global != NULL) {

        PyObject* global_type;
        if (!PyType_Check(global)) {
        //if (PyInstance_Check(global)) {
            global_type = PyObject_GetAttrString(global, "__class__");
        }
        else {
            global_type = (PyObject*)Py_TYPE((PyObject*)global);
            Py_INCREF((PyObject*)global_type);
        }
        if (global_type == NULL) {
            Py_DECREF((PyObject*)ctx_type);
            Py_DECREF((PyObject*)self);
            return NULL;
        }
        py_class* templ;
        templ = (py_class*)py_class_to_template(global_type, self);
        Py_XDECREF((PyObject*)global_type);
        if (templ == NULL) {
            Py_DECREF((PyObject*)ctx_type);
            Py_DECREF((PyObject*)self);
            return NULL;
        }
        Local<FunctionTemplate> global_inteface_template = templ->templ.Get(isolate);
        global_template = global_inteface_template->InstanceTemplate();
        //: Keep the class for ordered installation of global members.
        self->global_template = (PyObject*)templ;

    }

    IN_CONTEXT(Context::New(isolate, NULL, global_template, reused,
                           DeserializeInternalFieldsCallback(), microtasks));
    if (py_microtasks != Py_None) {
        self->microtask_queue = PyV8_NewRef(py_microtasks);
        ++((microtask_queue_c*)py_microtasks)->contexts;
    }
    self->js_context.Set(isolate_state, isolate, context);
    self->has_done = true;

    // add env to vm
    //IN_CONTEXT(self->js_context.Get(isolate));
    context->SetEmbedderData(CTX_TYPE, V8String(isolate, PyUnicode_AsUTF8_abi3(ctx_type)));
    Py_DECREF((PyObject*)ctx_type);
    if (!self->resources) self->resources = new ContextResources();
    if (!self->resources->lifetime) self->resources->lifetime = std::make_shared<ContextLifetime>(self);
    context->SetEmbedderData(CONTEXT_OBJECT_SLOT, v8_compat::external_new(isolate, self->resources->lifetime.get()));
    context->SetEmbedderData(OBJECT_PROTOTYPE_SLOT, Object::New(isolate)->GetPrototype());

    if (global != NULL) {
        if (!py_class_init_js_object(
                v8_compat::global_template_object(context), global, context)) {
            Py_DECREF((PyObject*)self);
            return NULL;
        }
    }
    return (PyObject *) self;
}

// Every Python reference this object owns, walked by both traverse and clear so
// the two lists cannot disagree.
static void context_clear_python_fields(context_c* self)
{
#define PYV8_CLEAR_FIELD(name) Py_CLEAR(self->name);
    CONTEXT_PY_FIELDS(PYV8_CLEAR_FIELD)
#undef PYV8_CLEAR_FIELD
}

void
context_finish_close(context_c *self)
{
    self->state = ContextState::Closing;
    wasm_stream_cancel_context(self);
    if (self->isolate_state && self->isolate_state->is_active())
        self->isolate_state->watchdog().release_owner(self);
    debugger_context_closed(self);
    if (self->resources) self->resources->unlink_dependencies(self);
    else Py_CLEAR(self->dependencies);
    if (self->isolate_state != nullptr) {
        self->isolate_state->invalidate_callbacks(self);
        if (!self->js_context.IsEmpty())
            self->js_context.Get(self->isolate_state->isolate())->SetEmbedderData(
                CONTEXT_OBJECT_SLOT, v8_compat::external_new(self->isolate_state->isolate(), nullptr));
        self->isolate_state->release_context_owners(self);
    }
    if (self->resources != nullptr) {
        while (!self->resources->handles.empty()) {
            auto item = *self->resources->handles.begin();
            self->resources->handles.erase(item.first);
            Py_INCREF(item.first);
            item.second(item.first);
            Py_DECREF(item.first);
        }
        delete self->resources;
        self->resources = nullptr;
    }
    // The V8 side goes first: the references below are Python's, and dropping
    // them while the realm they came from is still standing is exactly what
    // closing is meant to avoid.
    if (self->microtask_queue) {
        if (!self->js_context.IsEmpty())
            self->js_context.Get(self->isolate_state->isolate())->SetMicrotaskQueue(nullptr);
        microtask_queue_detach(self->microtask_queue);
    }
    self->js_context.Reset();
    delete self->trace_source_filter;
    self->trace_source_filter = nullptr;
    context_clear_python_fields(self);
    self->isolate_state = nullptr;
    self->state = ContextState::Closed;
}

static int context_traverse(context_c* self, visitproc visit, void* arg) {
#define PYV8_VISIT_FIELD(name) Py_VISIT(self->name);
    CONTEXT_PY_FIELDS(PYV8_VISIT_FIELD)
#undef PYV8_VISIT_FIELD
    if (self->isolate_state != nullptr)
        return self->isolate_state->traverse_context_owners(self, visit, arg);
    return 0;
}

static int context_clear(context_c* self) {
    if (self->state != ContextState::Active) return 0;
    auto* state = self->isolate_state;
    if (state == nullptr) { context_finish_close(self); return 0; }
    IsolateCleanupScope cleanup(state, (PyObject*)self, (int (*)(PyObject*))context_clear);
    if (!cleanup.ready()) return 0;
    auto* isolate = state->isolate();
    USING_V8;
    self->state = ContextState::Closing;
    // Legacy context deadlines end at logical close; active operation tickets
    // remain cancellable until their stack exits and actual disposal completes.
    state->watchdog().release_owner(self, true);
    // Callback frames still borrow binding metadata until the outer entry exits.
    if (!state->defer_context_close(self))
        context_finish_close(self);
    return 0;
}

PyObject*
context_close(context_c *self, PyObject* ignored)
{
    (void)ignored;
    if (!microtask_queue_check(self->microtask_queue)) return nullptr;
    if (!debugger_context_can_close(self)) return nullptr;
    // Explicit close keeps its synchronous contract; only GC cleanup is deferred.
    std::unique_ptr<IsolateLockScope> lock;
    if (self->isolate_state) {
        lock.reset(new IsolateLockScope(self->isolate_state));
        if (!lock->entered()) return nullptr;
    }
    context_clear(self);
    PYV8_RETURN_NONE;
}

void
context_dealloc(context_c *self)
{
    PyObject_GC_UnTrack(self);
    context_clear(self);
    if (Py_REFCNT((PyObject*)self) != 0) return;
    TP_FREE_ABI3(context_type, self);
}


static PyObject*
context_freeze(context_c* self, PyObject* value)
{
    ExecutionScope execution(self);
    if (!execution) return nullptr;
    auto* isolate = execution.isolate();
    Local<Context> context = self->js_context.Get(isolate);
    Local<Value> js_value = js_from_py(value, context);
    if (js_value.IsEmpty() || PyErr_Occurred())
        return nullptr;
    if (!js_value->IsObject())
        return py_from_js(js_value, context);
    Local<Object> object = js_value.As<Object>();
    JS_TRY
    DeadlineScope guard(context);
    guard.start();
    bool frozen = without_gil([&] {
        return object->SetIntegrityLevel(context, IntegrityLevel::kFrozen).FromMaybe(false);
    });
    guard.stop();
    PY_PROPAGATE_JS;
    if (!frozen) {
        PyErr_SetString(PyExc_RuntimeError, "failed to freeze JavaScript object");
        return nullptr;
    }
    return py_from_js(object, context);
}

PyObject*
context_js_obj(context_c* self, PyObject* py_object)
{
    if (!microtask_queue_check(self->microtask_queue)) return nullptr;
    // A pair that already has a handle is answered from the table alone; only
    // building the first one enters the isolate.
    if (!context_is_active(self)) {
        PyErr_SetString(PyExc_RuntimeError, "V8 context is closed");
        return nullptr;
    }

    PyObject* wrapper = self->isolate_state->cached_js_wrapper(self, py_object);
    if (wrapper == nullptr) {
        if (PyErr_Occurred()) return nullptr;
        PYV8_RETURN_NONE;
    }
    return wrapper;
}

PyObject *
context_run(context_c *self, PyObject *args, PyObject *kwargs)
{
    ExecutionScope execution(self);
    if (!execution) return nullptr;
    auto* isolate = execution.isolate();
    auto* isolate_state = self->isolate_state;
    PyObject *program;
    PyObject *filename = Py_None;
    double timeout = self->timeout;
    PyObject* py_timeout = Py_None;
    static const char *keywords[] = {"program", "filename", "timeout", NULL};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "O|OO", (char **) keywords, &program, &filename, &py_timeout)) {
        return NULL;
    }
    if (py_timeout != Py_None) timeout = PyFloat_AsDouble(py_timeout);
    if (PyErr_Occurred() || !valid_timeout(timeout)) return nullptr;
    const bool source_program = PyUnicode_Check(program);
    if (!source_program && !PyObject_TypeCheck(program, script_type))
    {
        PyErr_SetString(PyExc_TypeError, "program must be a string or Script");
        return NULL;
    }
    // Reject malformed or foreign programs before consuming an idle termination
    // request: only a valid execution may begin the next crossing.
    if (source_program) {
        if (filename != Py_None && !PyUnicode_Check(filename)) {
            PyErr_SetString(PyExc_TypeError, "filename must be a string or None");
            return nullptr;
        }
    } else {
        auto* candidate = (script_c*)program;
        if (candidate->script.IsEmpty()) {
            PyErr_SetString(PyExc_RuntimeError, "V8 script context is closed");
            return nullptr;
        }
        if (candidate->isolate_state != isolate_state) {
            PyErr_SetString(PyExc_RuntimeError, "JavaScript value belongs to another isolate");
            return nullptr;
        }
    }
    // Compilation and execution spend the same deadline. The internal factory
    // borrows this scope without registering another crossing; the public constructor
    // instead applies the Context default when called independently.
    DeadlineScope timeout_guard(timeout, isolate);
    timeout_guard.start();
    if (source_program)
    {
        program = script_from_source(script_type, self, program, filename, &timeout_guard);
        PyErr_PROPAGATE(program);
    }
    else
    {
        Py_INCREF((PyObject*)program);
    }
    assert(PyObject_TypeCheck(program, script_type));
    script_c *py_script = (script_c *) program;

    IN_CONTEXT(self->js_context.Get(isolate));
    JS_TRY

    Local<UnboundScript> unbound_script = py_script->script.Get(isolate);
    Py_XDECREF((PyObject*)program);
    Local<Script> script = unbound_script->BindToCurrentContext();

    debugger_prepare_script(self);
    MaybeLocal<Value> result = without_gil([&] { return script->Run(context); });

    timeout_guard.stop();
    PY_PROPAGATE_JS;
    if (timeout_guard.terminated()) {
        PyErr_SetNone((PyObject*)js_terminated_type);
        return nullptr;
    }
    Local<Value> js_result;
    if (!result.ToLocal(&js_result)) {
        PY_PROPAGATE_JS;
        PyErr_SetString(PyExc_RuntimeError, "failed to execute JavaScript");
        return NULL;
    }
    return py_from_js(js_result, context);
}


PyObject*
context_get_trace(context_c* self, void*)
{
    return PyBool_FromLong(self->trace);
}

int
context_set_trace(context_c* self, PyObject* value, void*)
{
    if (value == nullptr || !PyBool_Check(value)) {
        PyErr_SetString(PyExc_TypeError, "trace must be a bool");
        return -1;
    }
    ExecutionScope execution(self, value);
    if (!execution) {
        // Keep this property's established closed-context error while sharing
        // the guarded entry and its post-lock lifecycle validation.
        if (!context_is_active(self)) context_require_isolate(self);
        return -1;
    }
    if (value == Py_True && !self->traceable) {
        PyErr_SetString(PyExc_RuntimeError, "create Context with trace=True or traceable=True before enabling trace");
        return -1;
    }
    self->trace = value == Py_True;
    return 0;
}

PyObject *
context_get_timeout(context_c *self, void *ignored)
{
    return PyFloat_FromDouble(self->timeout);
}

int
context_set_timeout(context_c *self, PyObject *value, void *ignored)
{
    double timeout = PyFloat_AsDouble(value);
    if (PyErr_Occurred())
        return -1;
    if (!valid_timeout(timeout)) return -1;
    self->timeout = timeout;
    return 0;
}

PyObject *
context_get_global(context_c *self, void *ignored)
{
    ExecutionScope execution(self);
    if (!execution) return nullptr;
    auto* isolate = execution.isolate();
    Local<Context> context = self->js_context.Get(isolate);
    PyObject* pyobj = py_from_js(
        v8_compat::global_template_object(context), context);
    PyErr_PROPAGATE(pyobj);
    PyObject* result = context_js_obj(self, pyobj);
    Py_DECREF((PyObject*)pyobj);
    return result;
}

PyObject *
context_getattro(context_c *self, PyObject *name)
{
    PyObject *value = PyObject_GenericGetAttr((PyObject *) self, name);
    if (value == NULL) {
        PyErr_Clear();
        return context_getitem(self, name);
    }
    return value;
}

PyObject *
context_getitem(context_c *self, PyObject *name)
{
    PyObject* global = context_get_global(self, NULL);
    PyErr_PROPAGATE(global);
    PyObject* result = PyObject_GetAttr(global, name);
    Py_DECREF((PyObject*)global);
    return result;
}

int
context_setattro(context_c *self, PyObject *name, PyObject *value)
{
    // use GenericGetAttr to find out if Context defines the property
    PyObject *ctx_value = PyObject_GenericGetAttr((PyObject *) self, name);
    if (ctx_value == NULL) {
        // if the property is not defined by Context, set it on the global
        PyErr_Clear();
        return context_setitem(self, name, value);
    } else {
        Py_XDECREF((PyObject*)ctx_value);
    }
    // if the property is defined by Context, delegate
    return PyObject_GenericSetAttr((PyObject *) self, name, value);
}

int
context_setitem(context_c *self, PyObject *name, PyObject *value)
{
    PyObject *global = context_get_global(self, NULL);
    PyErr_PROPAGATE_(global);
    int result = PyObject_SetAttr(global, name, value);
    Py_DECREF((PyObject*)global);
    return result;
}

PyObject *
context_gc(context_c *self)
{
    ExecutionScope execution(self);
    if (!execution) return nullptr;
    auto* isolate = execution.isolate();
    isolate->LowMemoryNotification();
    PYV8_RETURN_NONE;
}

PyObject *
context_enqueue_microtask(context_c *self, PyObject *function)
{
#ifndef V8_CPPGC_MICROTASK_QUEUE
    // The macro decides MicrotaskQueue's layout and only the library's own command
    // line defines it: built without it, every virtual call would reach the wrong
    // slot.
    PyErr_SetString(PyExc_RuntimeError,
                    "this V8 build does not use cppgc microtask queues");
    return NULL;
#else
    ExecutionScope execution(self);
    if (!execution) return nullptr;
    auto* isolate = execution.isolate();
    IN_CONTEXT(self->js_context.Get(isolate));
    Local<Value> microtask = js_from_py(function, context);
    if (microtask.IsEmpty() || PyErr_Occurred())
        return NULL;
    if (!microtask->IsFunction()) {
        PyErr_SetString(PyExc_TypeError, "microtask must be a function");
        return NULL;
    }
    MicrotaskQueue* queue = context->GetMicrotaskQueue();
    if (queue == nullptr) {
        PyErr_SetString(PyExc_RuntimeError, "V8 context has no microtask queue");
        return NULL;
    }
    queue->EnqueueMicrotask(isolate, microtask.As<Function>());
    PYV8_RETURN_NONE;
#endif
}

static PyObject* context_set_microtask_queue(context_c* self, PyObject* queue) {
    auto* state = context_require_isolate(self);
    if (!state || !microtask_queue_check(self->microtask_queue)) return nullptr;
    if (queue == Py_None) {
        PyErr_SetString(PyExc_TypeError, "queue must be a MicrotaskQueue"); return nullptr;
    }
    auto* native = microtask_queue_get(queue, state);
    if (PyErr_Occurred()) return nullptr;
    if (queue == self->microtask_queue) PYV8_RETURN_NONE;
    IN_ISOLATE(state);
    auto context = self->js_context.Get(isolate);
    auto* previous_queue = context->GetMicrotaskQueue();
    if (!isolate->GetEnteredOrMicrotaskContext().IsEmpty() ||
        previous_queue->IsRunningMicrotasks() || previous_queue->GetMicrotasksScopeDepth()) {
        PyErr_SetString(PyExc_RuntimeError, "cannot change queue while a context or microtask is entered"); return nullptr;
    }
    context->SetMicrotaskQueue(native);
    if (self->microtask_queue) microtask_queue_detach(self->microtask_queue);
    PyObject* previous = self->microtask_queue;
    self->microtask_queue = PyV8_NewRef(queue);
    ++((microtask_queue_c*)queue)->contexts;
    Py_XDECREF(previous);
    PYV8_RETURN_NONE;
}

PyObject *
context_set_error_hooks(context_c *self, PyObject *args, PyObject *kwargs)
{
    // Each call replaces both reporters for this Context only.
    PyObject *on_uncaught = Py_None;
    PyObject *on_rejection = Py_None;
    static const char *keywords[] = {"on_uncaught", "on_rejection", NULL};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "|OO", (char **) keywords,
                                     &on_uncaught, &on_rejection)) {
        return NULL;
    }
    ExecutionScope execution(self);
    if (!execution) return nullptr;
    if (!error_hooks_install(self, on_uncaught, on_rejection))
        return NULL;
    PYV8_RETURN_NONE;
}

PyObject*
context_to_js(context_c* self, PyObject* value)
{
    // Converts a Python value into this realm: the structural counterpart of
    // `pyv8.transfer`, and the reason a transfer needs no JavaScript trampoline.
    ExecutionScope execution(self);
    if (!execution) return nullptr;
    Local<Context> base = execution.context();
    Local<Value> converted = js_from_py(value, base);
    if (converted.IsEmpty() || PyErr_Occurred())
        return nullptr;
    return py_from_js(converted, base);
}

PyObject*
context_import_handle(context_c* self, PyObject* handle)
{
    // A handle from another realm of the same isolate: taking a reference on its
    // realm here is what keeps that realm alive until this one closes.
    ExecutionScope execution(self, handle);
    if (!execution) return nullptr;
    auto* state = self->isolate_state;
    PyObject* source = nullptr;
    if (PyObject_TypeCheck(handle, js_object_type))
    {
        js_object* value = (js_object*)handle;
        if (value->object.IsEmpty() || value->isolate_state == nullptr ||
            (value->registration.context && !context_is_active((context_c*)value->registration.context))) {
            PyErr_SetString(PyExc_RuntimeError, "JavaScript value Context is closed");
            return nullptr;
        }
        if (!js_object_require_active(value))
            return nullptr;
        if (value->isolate_state != state)
        {
            PyErr_SetString(PyExc_RuntimeError,
                            "JavaScript value belongs to another isolate; "
                            "use pyv8.transfer() to copy it");
            return nullptr;
        }
        source = value->py_context;
    }
    else if (PyObject_TypeCheck(handle, js_value_type))
    {
        js_value* value = (js_value*)handle;
        if (value->value.IsEmpty() || value->isolate_state == nullptr ||
            (value->registration.context && !context_is_active(value->registration.context))) {
            PyErr_SetString(PyExc_RuntimeError, "JavaScript value Context is closed");
            return nullptr;
        }
        if (value->isolate_state != state)
        {
            PyErr_SetString(PyExc_RuntimeError,
                            "JavaScript value belongs to another isolate; "
                            "use pyv8.transfer() to copy it");
            return nullptr;
        }
        source = value->py_context;
    }
    else
    {
        PyErr_SetString(PyExc_TypeError, "import_handle expects a JSObject or JSValue");
        return nullptr;
    }
    if (source == nullptr || source == (PyObject*)self)
    {
        Py_INCREF(handle);
        return handle;
    }
    if (!context_retain_dependency(execution.context(), source))
        return nullptr;
    Py_INCREF(handle);
    return handle;
}

PyObject*
context_set_deadline(context_c* self, PyObject* args, PyObject* kwargs)
{
    PyObject* seconds = NULL;
    static const char* keywords[] = {"seconds", NULL};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "O", (char**)keywords, &seconds))
        return NULL;
    double value = PyFloat_AsDouble(seconds);
    if (PyErr_Occurred() || !valid_timeout(value))
        return NULL;
    auto* state = context_require_isolate(self);
    if (state == nullptr)
        return NULL;
    state->watchdog().arm(value, self);
    PYV8_RETURN_NONE;
}

static PyObject* context_begin_operation(context_c* self, PyObject* seconds) {
    auto* state = context_require_isolate(self);
    if (!state || !microtask_queue_check(self->microtask_queue)) return nullptr;
    const double value = PyFloat_AsDouble(seconds);
    if (PyErr_Occurred() || !valid_timeout(value)) return nullptr;
    if (value < 0) { PyErr_SetString(PyExc_ValueError, "timeout must be non-negative"); return nullptr; }
    return PyLong_FromUnsignedLongLong(state->watchdog().arm(value, self, true));
}

static PyObject* context_end_operation(context_c* self, PyObject* value) {
    const auto ticket = PyLong_AsUnsignedLongLong(value);
    if (PyErr_Occurred() || !microtask_queue_check(self->microtask_queue)) return nullptr;
    if (self->isolate_state) self->isolate_state->watchdog().disarm_ticket(ticket, self);
    PYV8_RETURN_NONE;
}

static PyObject* context_terminate_operation(context_c* self, PyObject* value) {
    const auto ticket = PyLong_AsUnsignedLongLong(value);
    if (PyErr_Occurred()) return nullptr;
    auto* state = self->isolate_state;
    return PyBool_FromLong(state && state->watchdog().terminate_ticket(ticket, self));
}

static PyObject* context_suspend_deadlines(context_c* self, PyObject*) {
    auto* state = context_require_isolate(self);
    if (!state || !microtask_queue_check(self->microtask_queue)) return nullptr;
    const auto tickets = state->watchdog().suspend_deadlines();
    PyObject* result = PyList_New(tickets.size());
    if (!result) { state->watchdog().resume_deadlines(tickets); return nullptr; }
    for (size_t index = 0; index < tickets.size(); ++index) {
        PyObject* ticket = PyLong_FromUnsignedLongLong(tickets[index]);
        if (!ticket) { Py_DECREF(result); state->watchdog().resume_deadlines(tickets); return nullptr; }
        PyList_SetItem(result, index, ticket);
    }
    return result;
}

static PyObject* context_resume_deadlines(context_c* self, PyObject* values) {
    if (!microtask_queue_check(self->microtask_queue)) return nullptr;
    Watchdog::Suspension tickets;
    PyOwnedRef iterator(PyObject_GetIter(values));
    if (!iterator.get()) return nullptr;
    while (PyObject* item = PyIter_Next(iterator.get())) {
        const auto ticket = PyLong_AsUnsignedLongLong(item);
        Py_DECREF(item);
        if (PyErr_Occurred()) return nullptr;
        tickets.push_back(ticket);
    }
    if (PyErr_Occurred()) return nullptr;
    if (self->isolate_state) self->isolate_state->watchdog().resume_deadlines(tickets);
    PYV8_RETURN_NONE;
}

PyObject*
context_clear_deadline(context_c* self, PyObject* ignored)
{
    (void)ignored;
    // A command clears the deadline of the context it borrowed, and that command may
    // have closed the context while it ran.  The arm being undone belongs to the
    // isolate's watchdog, which is still there, so it is disarmed through the isolate
    // rather than through the context: leaving it armed would fire on whatever runs
    // next, long after the command that asked for it finished.
    IsolateState* state = self->isolate_state;
    if (state != nullptr && state->is_active())
        state->watchdog().disarm(self);
    PYV8_RETURN_NONE;
}

PyObject*
context_terminate(context_c* self, PyObject* ignored)
{
    (void)ignored;
    auto* state = context_require_isolate(self);
    if (state == nullptr)
        return NULL;
    state->watchdog().terminate_now();
    PYV8_RETURN_NONE;
}

// The Context boundary owns JavaScript exception conversion for this task.
static PyObject* context_run_foreground_task(context_c* self, PyObject*) {
    auto* active = v8::Isolate::GetCurrent();
    if (active && active->InContext()) {
        PyErr_SetString(PyExc_RuntimeError, "cannot pump V8 foreground tasks during a Context entry");
        return nullptr;
    }
    ExecutionScope entry(self);
    if (!entry) return nullptr;
    auto* isolate = entry.isolate();
    JS_TRY;
    bool worked = false;
    without_gil([&] { worked = runtime_manager.pump_foreground_task(isolate); });
    PY_PROPAGATE_JS;
    return PyBool_FromLong(worked);
}

PyObject* microtask_queue_checkpoint(microtask_queue_c* self, PyObject* args, PyObject* kwargs) {
    double timeout = 0;
    static const char* names[] = {"timeout", nullptr};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "|d", (char**)names, &timeout)) return nullptr;
    if (!std::isfinite(timeout) || timeout < 0 || timeout > 1e9) {
        PyErr_SetString(PyExc_ValueError, "timeout must be finite and in 0..1e9"); return nullptr;
    }
    auto* queue = microtask_queue_get((PyObject*)self, self->state);
    if (!queue) return nullptr;
    IN_ISOLATE(self->state);
    JS_TRY;
    DeadlineScope budget(timeout, isolate);
    budget.start();
    without_gil([&] { queue->PerformCheckpoint(isolate); });
    budget.stop();
    PY_PROPAGATE_JS;
    Py_RETURN_NONE;
}
