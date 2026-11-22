#include "bridge_base.h"
#include <Python.h>
#include "pyv8.h"
#include "idl_spec.h"
#include <v8.h>
#include <cstring>
#include <string>

#include "context.h"
#include "convert.h"
#include "error_family.h"
#include "script.h"
#include "pyclass.h"
#include "jsobject.h"
#include "jsvalue.h"
#include "jsfunction.h"
#include "jspromise.h"
#include "serialization.h"
#include "jstypedarray.h"
#include "jsbuffer.h"
#include "pyfunction.h"
#include "debugger.h"
#include "isolate.h"
#include "watchdog.h"

#ifdef V8_OS_WIN
#define LIB_EXPORT __declspec(dllexport)
#else // V8_OS_WIN
#define LIB_EXPORT __attribute__((visibility("default")))
#endif

using namespace v8;

static PyObject *
py_initialize_v8(PyObject *self, PyObject *args, PyObject *kw)
{
    static const char *keywords[] = {"icudtl_path", "params", NULL};
    PyObject *icu = NULL;
    PyObject *params = NULL;

    if (!PyArg_ParseTupleAndKeywords(args, kw, "|OO", (char**)keywords, &icu, &params))
    {
        return NULL;
    }
    bool initialized = runtime_manager.initialize(icu, params);
    if (PyErr_Occurred())
        return NULL;
    return PyBool_FromLong(initialized);
}

static PyObject *
py_shutdown_v8(PyObject *self, PyObject *args)
{
    bool shut_down = runtime_manager.shutdown(true);
    if (PyErr_Occurred())
        return NULL;
    return PyBool_FromLong(shut_down);
}

// The public way to say what the next startup should use.  Startup itself is left
// to the first entry point that needs V8, so importing the package starts nothing.
static PyObject *
py_configure(PyObject *self, PyObject *args, PyObject *kw)
{
    static const char *keywords[] = {"icudtl_path", "params", NULL};
    PyObject *icu = NULL;
    PyObject *params = NULL;

    if (!PyArg_ParseTupleAndKeywords(args, kw, "|OO", (char**)keywords, &icu, &params))
    {
        return NULL;
    }
    if (!runtime_manager.configure(icu, params))
        return NULL;
    return PyBool_FromLong(true);
}

static PyObject *
py_is_initialized(PyObject *self, PyObject *args)
{
    return PyBool_FromLong(runtime_manager.is_initialized());
}

static PyObject* mark_spec_flag(PyObject* thing, const char* key)
{
    PyObject* original = idl_spec_for(thing);
    if (original == nullptr && PyErr_Occurred()) return nullptr;
    if (original == nullptr) original = PyObject_CallObject((PyObject*)idl_spec_type, nullptr);
    if (original == nullptr) return nullptr;
    PyObject* changes = PyDict_New();
    if (changes == nullptr) { Py_DECREF((PyObject*)original); return nullptr; }
    int result = PyDict_SetItemString(changes, key, Py_True);
    PyObject* spec = result == 0 ? idl_spec_replace(original, changes) : nullptr;
    Py_DECREF((PyObject*)changes);
    Py_DECREF((PyObject*)original);
    if (spec == nullptr) return nullptr;
    result = PyObject_SetAttrString(thing, "__v8_idl_spec__", spec);
    Py_DECREF((PyObject*)spec);
    if (result < 0) return NULL;
    Py_INCREF((PyObject*)thing);
    return thing;
}

PyObject *
mark_hidden(PyObject *self, PyObject *thing)
{
    return mark_spec_flag(thing, "hidden");
}

PyObject *
mark_unconstructable(PyObject *self, PyObject *thing)
{
    return mark_spec_flag(thing, "unconstructable");
}

PyObject *
trigger_low_memory_notification(PyObject *self, PyObject *args)
{
    if (!runtime_manager.ensure_initialized())
        return NULL;
    IsolateState* isolate_state = select_isolate(IsolateSelection::DefaultOnly);
    IN_ISOLATE(isolate_state);
    isolate->LowMemoryNotification();
    PYV8_RETURN_NONE;
}

PyObject *
construct_new_object(PyObject *self, PyObject *args)
{
    if (!runtime_manager.ensure_initialized())
        return NULL;
    int argc = (int)PyTuple_Size(args);
    if (argc == 0)
    {
        PyErr_SetString(PyExc_TypeError, "First argument must be a constructor function.");
        return NULL;
    }

    PyObject *constructor = PyTuple_GetItem(args, 0);
    if (!PyObject_TypeCheck(constructor, js_function_type))
    {
        PyErr_SetString(PyExc_TypeError, "First argument must be a constructor function.");
        return NULL;
    }

    js_function *function = (js_function *)constructor;

    IsolateState* isolate_state = function->base.isolate_state;
    if (isolate_state == nullptr || !isolate_state->require_active())
        return NULL;
    IN_ISOLATE(isolate_state);
    Local<Object> object = function->base.object.Get(isolate);
    IN_CONTEXT(object->GetCreationContextChecked())
    JS_TRY

    if (!object->IsConstructor())
    {
        PyErr_SetString(PyExc_TypeError, "First argument must be a constructor function.");
        return NULL;
    }

    // exclude first argument
    argc--;

    MaybeLocal<Value> result;
    DeadlineScope timeout_guard(context);
    V8ValueArguments argv(argc);
    if (!argv.valid())
    {
        PyErr_NoMemory();
        return NULL;
    }
    for (long i = 0; i < argc; i++)
    {
        argv.data()[i] = js_from_py(PyTuple_GetItem(args, i + 1), context);
    }
    if (PyErr_Occurred())
        return NULL;
    timeout_guard.start();
    result = without_gil([&] {
        return object->CallAsConstructor(isolate->GetCurrentContext(), argc, argv.data());
    });

    timeout_guard.stop();

    PY_PROPAGATE_JS;

    Local<Value> js_result;
    if (!result.ToLocal(&js_result))
    {
        PY_PROPAGATE_JS;
        PyErr_SetString(PyExc_RuntimeError, "failed to construct JavaScript object");
        return NULL;
    }
    return py_from_js(js_result, context);
}

static PyObject*
query_context(PyObject* args, bool incumbent)
{
    if (!runtime_manager.ensure_initialized())
        return NULL;
    PyObject* py_isolate = Py_None;
    if (!PyArg_ParseTuple(args, "|O", &py_isolate))
        return NULL;
    // A worker/explicit isolate callback queries its own execution, not the
    // unrelated default isolate. Outside execution retain the default fallback.
    IsolateState* isolate_state = select_isolate(
        py_isolate == Py_None ? IsolateSelection::ActiveOrDefault : IsolateSelection::Explicit, py_isolate);
    if (isolate_state == nullptr)
        return NULL;
    // An idle query must not create a top-level Locker. V8 initializes its
    // thread state at that entry, which clears pending interrupt flags such as
    // a background compiler's global-safepoint or code-install request.
    if (!IsolateLockScope::has_thread_entry(isolate_state)) {
        if (PyErr_Occurred()) return nullptr;
        PYV8_RETURN_NONE;
    }
    IN_ISOLATE(isolate_state);
    Local<Context> current_context = incumbent ? isolate->GetIncumbentContext()
                                             : isolate->GetCurrentContext();
    if (current_context.IsEmpty())
    {
        PYV8_RETURN_NONE;
    }
    // A context the engine is running in need not be one this binding owns: a
    // realm whose owner has already been released, or one V8 itself made, answers
    // with nothing here.  Answering "no context" is what the caller expects from
    // an empty stack; taking a reference to it is not.
    PyObject *context = (PyObject*)context_from_v8(current_context);
    if (context == nullptr)
    {
        PYV8_RETURN_NONE;
    }
    Py_INCREF((PyObject*)context);
    return context;
}

PyObject*
context_get_current(PyObject* self, PyObject* args)
{
    return query_context(args, false);
}

PyObject*
context_get_incumbent(PyObject* self, PyObject* args)
{
    return query_context(args, true);
}

PyObject *
v8_version(PyObject *self, PyObject *args)
{
    const char *version = v8::V8::GetVersion();
    return PyUnicode_FromString(version);
}

PyObject *
v8_gc(PyObject *self, PyObject *args)
{
    if (!runtime_manager.ensure_initialized())
        return NULL;
    IsolateState* isolate_state = select_isolate(IsolateSelection::DefaultOnly);
    IN_ISOLATE(isolate_state);
    isolate->LowMemoryNotification();
    PYV8_RETURN_NONE;
}

static PyMethodDef v8_methods[] = {
    {"hidden", (PyCFunction)PythonBoundary<mark_hidden>::call, METH_O, ""},
    {"unconstructable", (PyCFunction)PythonBoundary<mark_unconstructable>::call, METH_O, ""},
    {"current_context", (PyCFunction)PythonBoundary<context_get_current>::call, METH_VARARGS, "get current v8 context"},
    {"incumbent_context", (PyCFunction)PythonBoundary<context_get_incumbent>::call, METH_VARARGS, "get incumbent v8 context"},
    {"new", (PyCFunction)PythonBoundary<construct_new_object>::call, METH_VARARGS, "Creates a new JavaScript object from a given constructor function"},
    {"low_memory_notification", (PyCFunction)PythonBoundary<trigger_low_memory_notification>::call, METH_NOARGS, "Trigger V8 low memory notification"},
    {"init_v8", (PyCFunction)PythonBoundary<py_initialize_v8>::call, METH_VARARGS | METH_KEYWORDS, "initialize v8 platform and isolate"},
    {"shutdown_v8", (PyCFunction)PythonBoundary<py_shutdown_v8>::call, METH_NOARGS, "shut down v8 platform and isolate"},
    {"configure", (PyCFunction)PythonBoundary<py_configure>::call, METH_VARARGS | METH_KEYWORDS, "set the ICU data file and V8 flags the next startup should use"},
    {"is_initialized", (PyCFunction)PythonBoundary<py_is_initialized>::call, METH_NOARGS, "whether v8 has been started"},
    {"v8_version", (PyCFunction)PythonBoundary<v8_version>::call, METH_NOARGS, "v8 version"},
    {"v8_gc", (PyCFunction)PythonBoundary<v8_gc>::call, METH_NOARGS, "v8 gc"},
    {NULL},
};

static bool
require_supported_python_runtime()
{
#ifdef Py_GIL_DISABLED
    PyErr_SetString(
        PyExc_ImportError,
        "pyv8 does not support free-threaded CPython builds");
    return false;
#else
    PyThreadState* thread_state = PyThreadState_Get();
    PyInterpreterState* interpreter =
        PyThreadState_GetInterpreter(thread_state);
    int64_t interpreter_id = PyInterpreterState_GetID(interpreter);
    if (interpreter_id < 0)
        return false;
    if (interpreter_id != 0) {
        PyErr_SetString(
            PyExc_ImportError,
            "pyv8 only supports the main Python interpreter because its V8 runtime is process-global");
        return false;
    }
    return true;
#endif
}

// icudtl.dat ships beside the extension, so the module can name the default data
// file itself instead of the package handing one in at import.  Recording it is not
// starting V8: nothing is read or hashed until something first needs the runtime.
static int
record_default_icu_path(PyObject* module)
{
    PyObject* filename = PyModule_GetFilenameObject(module);
    if (filename == nullptr)
        return -1;

    const char* path = PyUnicode_AsUTF8_abi3(filename);
    if (path == nullptr)
    {
        Py_DECREF(filename);
        return -1;
    }

    std::string directory(path);
    const size_t separator = directory.find_last_of("\\/");
    if (separator == std::string::npos)
    {
        Py_DECREF(filename);
        PyErr_SetString(PyExc_ImportError,
                        "cannot locate icudtl.dat: _pyv8 was imported without a file path");
        return -1;
    }
    directory.resize(separator + 1);
    directory += "icudtl.dat";

    PyObject* icu_path =
        PyUnicode_FromStringAndSize(directory.data(), (Py_ssize_t)directory.size());
    Py_DECREF(filename);
    if (icu_path == nullptr)
        return -1;

    runtime_manager.set_default_icu_path(icu_path);
    Py_DECREF(icu_path);
    return 0;
}

static int
pyv8_module_exec(PyObject* module)
{
    if (!require_supported_python_runtime())
        return -1;

    if (record_default_icu_path(module) < 0)
        return -1;

    if (idl_spec_init(module) < 0)
        return -1;

    if (cache_FunctionType_init() < 0)
        return -1;

    if (isolate_type_init() < 0)
        return -1;
    if (PyModule_AddObject(module, "Isolate", (PyObject*)isolate_type) < 0)
        return -1;

    if (isolate_register_microtask_queue(module) < 0)
        return -1;
    if (context_type_init() < 0)
        return -1;
    if (PyModule_AddObject(module, "Context", (PyObject *)context_type) < 0)
        return -1;

    if (script_type_init() < 0)
        return -1;
    if (PyModule_AddObject(module, "Script", (PyObject *)script_type) < 0)
        return -1;

    if (debugger_type_init() < 0)
        return -1;
    if (PyModule_AddObject(module, "Debugger", (PyObject *)debugger_type) < 0)
        return -1;

    if (py_function_type_init() < 0)
        return -1;

    if (py_class_type_init() < 0)
        return -1;

    if (js_object_type_init() < 0)
        return -1;
    if (PyModule_AddObject(module, "JSObject", (PyObject *)js_object_type) < 0)
        return -1;

    if (js_value_type_init() < 0)
        return -1;
    if (PyModule_AddObject(module, "JSValue", (PyObject*)js_value_type) < 0)
        return -1;

    if (js_promise_type_init() < 0)
        return -1;
    if (PyModule_AddObject(module, "JSPromise", (PyObject *)js_promise_type) < 0)
        return -1;

    if (js_promise_resolver_type_init() < 0)
        return -1;
    if (PyModule_AddObject(module, "PromiseResolver", (PyObject *)js_promise_resolver_type) < 0)
        return -1;

    if (js_typedarray_type_init() < 0)
        return -1;
    if (PyModule_AddObject(module, "JSTypeArray", (PyObject *)js_typedarray_type) < 0)
        return -1;

    if (js_buffer_type_init() < 0)
        return -1;
    if (PyModule_AddObject(module, "JSBuffer", (PyObject *)js_buffer_type) < 0)
        return -1;

    if (js_function_type_init() < 0)
        return -1;

    if (PyModule_AddObject(module, "JSFunction", (PyObject *)js_function_type) < 0)
        return -1;

    if (js_exception_type_init() < 0)
        return -1;
    if (PyModule_AddObject(module, "JSException", (PyObject *)js_exception_type) < 0)
        return -1;

    if (js_terminated_type_init() < 0)
        return -1;
    if (PyModule_AddObject(module, "JavaScriptTerminated", (PyObject *)js_terminated_type) < 0)
        return -1;

    if (js_null_type_init() < 0)
        return -1;
    if (PyModule_AddObject(module, "Null", js_null_object) < 0)
        return -1;

    if (js_undefined_type_init() < 0)
        return -1;
    if (PyModule_AddObject(module, "Undefined", js_undefined_object) < 0)
        return -1;

    // The family publishes itself: the table in error_family.cpp is the only copy of
    // these names.
    if (!error_family::publish(module))
        return -1;

    // One realm's structured-clone wire: V8's own serializer plus the host-object
    // callbacks, which is what Chromium's structuredClone is made of.
    if (serialization_type_init() < 0)
        return -1;
    if (PyModule_AddObject(module, "Serializer", (PyObject*)serialization_type) < 0)
        return -1;
    if (PyModule_AddObject(module, "SerializationWriter", (PyObject*)serialization_writer_type) < 0)
        return -1;
    if (PyModule_AddObject(module, "SerializationReader", (PyObject*)serialization_reader_type) < 0)
        return -1;

    return 0;
}

static PyModuleDef_Slot v8_module_slots[] = {
    {Py_mod_exec, (void*)pyv8_module_exec},
    {0, NULL},
};

static PyModuleDef v8_module_def = {
    PyModuleDef_HEAD_INIT,
    "_pyv8",
    "V8 bindings for the main, GIL-enabled CPython interpreter.",
    0,
    v8_methods,
    v8_module_slots,
    NULL,
    NULL,
    NULL,
};

PyMODINIT_FUNC PyInit__pyv8()
{
    // Multi-phase initialization runs pyv8_module_exec in every interpreter,
    // allowing the process-global RuntimeManager policy to be enforced.
    return PyModuleDef_Init(&v8_module_def);
}
