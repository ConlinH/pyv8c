
#include "bridge_base.h"
#include "buffer_range.h"
#include <Python.h>
#include <cstring>
#include "entry.h"
#include "execution.h"
#include "runtime.h"
#include "crossing.h"
#include <v8.h>

#include "convert.h"
#include "jsbuffer.h"
#include "jsobject.h"
#include "context.h"
#include "jsfunction.h"
#include "jspromise.h"
#include "jstypedarray.h"
#include "watchdog.h"

using namespace v8;


//: What every handle method opens with: the handle must be live, its isolate and realm
//: entered, and the object it stands for read out.  *self* is the handle; the other two
//: name the locals the caller then uses.  It lives here rather than in a header because
//: only this file expands it, and because its body needs ``ExecutionScope`` and
//: ``context_c``, which ``jsobject.h`` cannot include without a cycle through ``context.h``.
#define JS_OBJECT_ENTER(self, isolate, object)                                  \
    if (!js_object_require_active(self)) return nullptr;                        \
    ExecutionScope execution((context_c*)(self)->registration.context,           \
                             (PyObject*)(self));                                \
    if (!execution || !js_object_require_active(self)) return nullptr;           \
    auto* isolate = execution.isolate();                                         \
    Local<Object> object = (self)->object.Get(isolate)


static PyObject* js_object_read(js_object* self, PyObject* name) {
    JS_OBJECT_ENTER(self, isolate, object);
    IN_CONTEXT(object->GetCreationContextChecked());
    Local<Value> key = js_from_py(name, context);
    if (key.IsEmpty() || PyErr_Occurred()) return nullptr;
    JS_TRY
    DeadlineScope guard(context);
    guard.start();
    Local<Value> value;
    bool success = without_gil([&] { return object->Get(context, key).ToLocal(&value); });
    guard.stop();
    PY_PROPAGATE_JS;
    if (!success) {
        // Nothing was thrown, so this message is the only thing that can tell the
        // caller what went wrong: a NULL return with no exception is reported by
        // CPython as a SystemError, which hides the real reason.
        PyErr_SetString(PyExc_RuntimeError, "failed to read JavaScript property");
        return nullptr;
    }
    return py_from_js(value, context);
}

static PyObject* js_object_host_value(js_object* self, PyObject*) {
    JS_OBJECT_ENTER(self, isolate, object);
    IN_CONTEXT(object->GetCreationContextChecked());
    return py_from_js(object, context);
}

static PyObject* js_object_to_python(js_object* self, PyObject*) {
    JS_OBJECT_ENTER(self, isolate, object);
    IN_CONTEXT(object->GetCreationContextChecked());
    JS_TRY
    DeadlineScope guard(context);
    guard.start();
    PyObject* result = py_materialize(object, context);
    guard.stop();
    if (tc.HasCaught()) Py_XDECREF(result);
    PY_PROPAGATE_JS;
    return result;
}

PyObject* js_object_bytes(js_object* self, PyObject*) {
    JS_OBJECT_ENTER(self, isolate, object);
    IN_CONTEXT(object->GetCreationContextChecked());
    JS_TRY
    DeadlineScope guard(context);
    guard.start();
    auto range = buffer_range(object);
    guard.stop();
    PY_PROPAGATE_JS;
    return PyBytes_FromStringAndSize(range.data() ? range.data() : "",
                                     static_cast<Py_ssize_t>(range.length));
}

//: ``bytes(value)`` for a buffer or a view.  The one-read reader above answers ``b""`` for
//: anything else, which is a poor answer for the bytes protocol, so a value that holds no
//: bytes is refused the way Python refuses one.
static PyObject* js_object_as_bytes(js_object* self, PyObject* unused) {
    JS_OBJECT_ENTER(self, isolate, object);
    if (!object->IsArrayBuffer() && !object->IsSharedArrayBuffer() &&
        !object->IsArrayBufferView()) {
        // The limited API leaves the type opaque, so the message cannot name it.
        PyErr_SetString(PyExc_TypeError,
                        "JavaScript object holds no bytes: not a buffer or a view");
        return nullptr;
    }
    return js_object_bytes(self, unused);
}

static PyObject* js_object_buffer_kind(js_object* self, PyObject*) {
    JS_OBJECT_ENTER(self, isolate, object);
    if (object->IsArrayBuffer()) {
        auto buffer = object.As<ArrayBuffer>();
        return PyUnicode_FromString(buffer->WasDetached() ? "detached" :
                                    buffer->IsDetachable() ? "buffer" : "nontransferable");
    }
    if (object->IsSharedArrayBuffer()) return PyUnicode_FromString("shared");
    if (object->IsArrayBufferView()) {
        auto buffer = object.As<ArrayBufferView>()->Buffer();
        if (buffer->IsSharedArrayBuffer()) return PyUnicode_FromString("shared-view");
        return PyUnicode_FromString(buffer->WasDetached() ? "detached" : "view");
    }
    Py_RETURN_NONE;
}

static PyObject* js_object_is_uint8_array(js_object* self, PyObject*) {
    JS_OBJECT_ENTER(self, isolate, object);
    return PyBool_FromLong(object->IsUint8Array());
}

static PyObject* js_object_array_buffer_view_info(js_object* self, PyObject*) {
    JS_OBJECT_ENTER(self, isolate, object);
    if (!object->IsArrayBufferView()) Py_RETURN_NONE;
    const char* kind = nullptr;
    if (object->IsDataView()) kind = "DataView";
    else if (object->IsUint8Array()) kind = "Uint8Array";
    else if (object->IsUint8ClampedArray()) kind = "Uint8ClampedArray";
    else if (object->IsInt8Array()) kind = "Int8Array";
    else if (object->IsUint16Array()) kind = "Uint16Array";
    else if (object->IsInt16Array()) kind = "Int16Array";
    else if (object->IsUint32Array()) kind = "Uint32Array";
    else if (object->IsInt32Array()) kind = "Int32Array";
    else if (object->IsBigInt64Array()) kind = "BigInt64Array";
    else if (object->IsBigUint64Array()) kind = "BigUint64Array";
    else if (object->IsFloat16Array()) kind = "Float16Array";
    else if (object->IsFloat32Array()) kind = "Float32Array";
    else if (object->IsFloat64Array()) kind = "Float64Array";
    else kind = "unknown";
    return Py_BuildValue("(sK)", kind,
                         static_cast<unsigned long long>(object.As<ArrayBufferView>()->ByteLength()));
}

static PyObject* js_object_write_buffer_bytes(js_object* self, PyObject* value) {
    char* data = nullptr;
    Py_ssize_t length = 0;
    if (PyBytes_AsStringAndSize(value, &data, &length) < 0) return nullptr;
    JS_OBJECT_ENTER(self, isolate, object);
    if (!object->IsArrayBufferView()) {
        PyErr_SetString(PyExc_TypeError, "value is not an ArrayBufferView");
        return nullptr;
    }
    auto range = buffer_range(object);
    if (range.length != static_cast<size_t>(length)) {
        PyErr_SetString(PyExc_ValueError, "buffer view length changed");
        return nullptr;
    }
    if (range.store && range.store->IsShared()) {
        PyErr_SetString(PyExc_ValueError, "shared buffers require indexed writes");
        return nullptr;
    }
    if (length) {
        if (!range.store || !range.store->Data()) {
            PyErr_SetString(PyExc_RuntimeError, "buffer view has no backing storage");
            return nullptr;
        }
        std::memcpy(static_cast<char*>(range.store->Data()) + range.offset,
                    data, static_cast<size_t>(length));
    }
    Py_RETURN_NONE;
}

static PyObject* js_object_freeze(js_object* self, PyObject*) {
    JS_OBJECT_ENTER(self, isolate, object);
    IN_CONTEXT(object->GetCreationContextChecked());
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
    Py_INCREF((PyObject*)self);
    return (PyObject*)self;
}

static PyObject* js_object_detach_buffer(js_object* self, PyObject*) {
    JS_OBJECT_ENTER(self, isolate, object);
    if (!object->IsArrayBuffer() || !object.As<ArrayBuffer>()->IsDetachable()) {
        PyErr_SetString(PyExc_TypeError, "value is not a detachable ArrayBuffer");
        return nullptr;
    }
    JS_TRY
    auto detached = object.As<ArrayBuffer>()->Detach(Local<Value>());
    PY_PROPAGATE_JS;
    if (detached.IsNothing() || !detached.FromJust()) {
        PyErr_SetString(PyExc_TypeError, "ArrayBuffer could not be detached");
        return nullptr;
    }
    Py_RETURN_NONE;
}

static PyObject* js_object_string(js_object* self, PyObject*) {
    return js_object_repr(self);
}

static PyObject* js_object_number(js_object* self, PyObject*) {
    JS_OBJECT_ENTER(self, isolate, object);
    IN_CONTEXT(object->GetCreationContextChecked());
    JS_TRY
    DeadlineScope guard(context);
    guard.start();
    Local<Number> value;
    bool success = without_gil([&] { return object->ToNumber(context).ToLocal(&value); });
    guard.stop();
    PY_PROPAGATE_JS;
    if (!success) {
        PyErr_SetString(PyExc_RuntimeError, "failed to convert JavaScript object to a number");
        return nullptr;
    }
    return PyFloat_FromDouble(value->Value());
}

static PyObject* js_object_iterator_method(js_object* self, PyObject*) {
    JS_OBJECT_ENTER(self, isolate, object);
    IN_CONTEXT(object->GetCreationContextChecked());
    JS_TRY
    DeadlineScope guard(context);
    guard.start();
    Local<Value> result;
    bool success = without_gil([&] { return object->Get(context, Symbol::GetIterator(isolate)).ToLocal(&result); });
    guard.stop();
    PY_PROPAGATE_JS;
    if (!success) {
        PyErr_SetString(PyExc_RuntimeError, "failed to read the JavaScript iterator");
        return nullptr;
    }
    return py_from_js(result, context);
}

static PyObject* js_object_invoke(js_object* self, PyObject* args) {
    JS_OBJECT_ENTER(self, isolate, object);
    IN_CONTEXT(object->GetCreationContextChecked());
    if (!object->IsCallable() || PyTuple_Size(args) < 1) {
        PyErr_SetString(PyExc_TypeError, "_invoke requires a callable and a receiver");
        return nullptr;
    }
    const int length = (int)PyTuple_Size(args);
    V8ValueArguments converted(length);
    if (!converted.valid()) { PyErr_NoMemory(); return nullptr; }
    jss_from_pys(args, converted.data(), context);
    if (PyErr_Occurred()) return nullptr;
    JS_TRY
    DeadlineScope guard(context);
    guard.start();
    Local<Value> result;
    bool success = without_gil([&] {
        return object->CallAsFunction(context, converted.data()[0], length - 1, converted.data() + 1).ToLocal(&result);
    });
    guard.stop();
    PY_PROPAGATE_JS;
    if (!success) {
        PyErr_SetString(PyExc_RuntimeError, "failed to invoke JavaScript callable");
        return nullptr;
    }
    return py_from_js(result, context);
}

static PyObject* js_object_keys(js_object* self, PyObject*) {
    JS_OBJECT_ENTER(self, isolate, object);
    IN_CONTEXT(object->GetCreationContextChecked());
    JS_TRY
    DeadlineScope guard(context);
    guard.start();
    Local<Array> keys;
    bool success = without_gil([&] {
        return object->GetOwnPropertyNames(context,
            static_cast<PropertyFilter>(ONLY_ENUMERABLE | SKIP_SYMBOLS),
            KeyConversionMode::kConvertToString).ToLocal(&keys);
    });
    guard.stop();
    PY_PROPAGATE_JS;
    if (!success) {
        PyErr_SetString(PyExc_RuntimeError, "failed to read JavaScript property names");
        return nullptr;
    }
    return py_materialize(keys, context);
}

static PyObject* js_object_is_array(js_object* self, PyObject*) {
    if (!js_object_require_active(self)) return nullptr;
    ExecutionScope execution((context_c*)self->registration.context, (PyObject*)self);
    if (!execution || !js_object_require_active(self)) return nullptr;
    auto* isolate = execution.isolate();
    return PyBool_FromLong(self->object.Get(isolate)->IsArray());
}

static PyObject* js_object_getitem(js_object* self, PyObject* key);

PyMethodDef js_object_methods[] = {
    {"_iterator_method", (PyCFunction)PythonBoundary<js_object_iterator_method>::call, METH_NOARGS, nullptr},
    {"_invoke", (PyCFunction)PythonBoundary<js_object_invoke>::call, METH_VARARGS, nullptr},
    {"_keys", (PyCFunction)PythonBoundary<js_object_keys>::call, METH_NOARGS, nullptr},
    {"_is_array", (PyCFunction)PythonBoundary<js_object_is_array>::call, METH_NOARGS, nullptr},
    {"_to_string", (PyCFunction)PythonBoundary<js_object_string>::call, METH_NOARGS, nullptr},
    {"_to_number", (PyCFunction)PythonBoundary<js_object_number>::call, METH_NOARGS, nullptr},
    {"_get", (PyCFunction)PythonBoundary<js_object_read>::call, METH_O, "Read one property without eagerly converting objects."},
    {"_host_value", (PyCFunction)PythonBoundary<js_object_host_value>::call, METH_NOARGS, "Unwrap host identity without reading properties."},
    {"to_python", (PyCFunction)PythonBoundary<js_object_to_python>::call, METH_NOARGS, "Explicitly materialize arrays and plain objects."},
    {"_bytes", (PyCFunction)PythonBoundary<js_object_bytes>::call, METH_NOARGS, "The bytes a buffer holds or a view looks at, in one read."},
    {"_buffer_kind", (PyCFunction)PythonBoundary<js_object_buffer_kind>::call, METH_NOARGS, "Native buffer brand and detached state without property access."},
    {"_is_uint8_array", (PyCFunction)PythonBoundary<js_object_is_uint8_array>::call, METH_NOARGS, "Check the native Uint8Array brand without property access."},
    {"_array_buffer_view_info", (PyCFunction)PythonBoundary<js_object_array_buffer_view_info>::call, METH_NOARGS, "Native ArrayBufferView brand and byte length without property access."},
    {"_write_buffer_bytes", (PyCFunction)PythonBoundary<js_object_write_buffer_bytes>::call, METH_O, "Copy bytes into a non-shared ArrayBufferView."},
    {"freeze", (PyCFunction)PythonBoundary<js_object_freeze>::call, METH_NOARGS, "Freeze this object in its owning V8 context."},
    {"_detach_buffer", (PyCFunction)PythonBoundary<js_object_detach_buffer>::call, METH_NOARGS, "Detach an ArrayBuffer without calling page code."},
    {"__bytes__", (PyCFunction)PythonBoundary<js_object_as_bytes>::call, METH_NOARGS, "The bytes a buffer or a view holds; anything else holds none."},
    {"_buffer_view", (PyCFunction)PythonBoundary<js_object_buffer>::call, METH_NOARGS, "**Experimental**. Return a python buffer(memoryview) object pointing to the underlying V8 ArrayBuffer data.  Underscore-prefixed because instance lookup only reaches those."},
    {"__dir__", (PyCFunction)PythonBoundary<js_object_dir>::call, METH_NOARGS, NULL},
    {NULL} };


static PyType_Slot js_object_slots[] = {
    {Py_tp_traverse, (void*)PythonBoundary<js_object_traverse>::call},
    {Py_tp_clear, (void*)PythonBoundary<js_object_clear>::call},
    {Py_tp_getattro, (void*)PythonBoundary<js_object_getattro>::call},    // __getattribute__
    {Py_tp_setattro, (void*)PythonBoundary<js_object_setattro>::call},    // __setattribute__
    {Py_mp_subscript, (void*)PythonBoundary<js_object_getitem>::call},  // __getitem__
    {Py_mp_ass_subscript, (void*)PythonBoundary<js_object_setattro>::call},  // __setitem__
    {Py_tp_iter, (void*)PythonBoundary<js_object_getiter>::call},
    {Py_tp_richcompare, (void*)PythonBoundary<js_object_richcompare>::call},
    {Py_tp_dealloc, (void*)PythonBoundary<js_object_dealloc>::call},
    {Py_tp_repr, (void*)PythonBoundary<js_object_repr>::call},
    {Py_tp_methods, js_object_methods},
    {Py_tp_doc, (void*)"v8 Object"},
    {0, NULL} };

static PyType_Spec js_object_spec = {
    "pyv8c.pyv8.JSObject",
    sizeof(js_object),
    0,
    Py_TPFLAGS_DEFAULT | Py_TPFLAGS_BASETYPE | Py_TPFLAGS_HAVE_GC,
    js_object_slots };


PyTypeObject *js_object_type = NULL;

bool
js_object_require_active(const js_object* self)
{
    if (self == nullptr || self->isolate_state == nullptr || self->object.IsEmpty() ||
        (self->registration.context && !context_is_active((context_c*)self->registration.context))) {
        PyErr_SetString(PyExc_RuntimeError, "JavaScript object has no active V8 isolate");
        return false;
    }
    return self->isolate_state->require_active();
}

static bool
js_object_should_try_python_attribute(js_object* self, PyObject* name)
{
    if (Py_TYPE((PyObject*)self) != js_object_type &&
        Py_TYPE((PyObject*)self) != js_function_type &&
        Py_TYPE((PyObject*)self) != js_promise_type &&
        Py_TYPE((PyObject*)self) != js_typedarray_type)
    {
        return true;
    }
    if (!PyUnicode_Check(name))
        return false;

    Py_ssize_t length;
    const char* utf8_name = PyUnicode_AsUTF8AndSize(name, &length);
    if (utf8_name == NULL)
        return true;
    return length == 0 || utf8_name[0] == '_' || PyUnicode_CompareWithASCIIString(name, "to_python") == 0;
}

void
js_object_set_persistent(js_object* self, Local<Object> object,
                         Local<Context> context, bool retain_context)
{
    Local<Context> creation;
    if (object->GetCreationContext().ToLocal(&creation)) context = creation;
    Isolate* isolate = v8_compat::context_isolate(context);
    IsolateState* isolate_state = runtime_manager.find_isolate(isolate);
    self->isolate_state = isolate_state;
    self->object.Set(isolate_state, isolate, object);
    if (!context.IsEmpty()) {
        Local<Value> context_data = context->GetEmbedderData(CONTEXT_OBJECT_SLOT);
        if (context_data->IsExternal()) {
            context_c* py_context = context_from_v8(context);
            if (py_context == nullptr) return;
            self->registration.attach(py_context, (PyObject*)self, (int (*)(PyObject*))js_object_clear);
            if (retain_context) {
                self->py_context = (PyObject*)py_context;
                Py_INCREF(self->py_context);
            }
            if (py_context->py_isolate != nullptr) {
                self->py_isolate = py_context->py_isolate;
                Py_INCREF(self->py_isolate);
            }
        }
    }
}

int js_object_type_init()
{
    js_object_type = (PyTypeObject*)PyType_FromSpec(&js_object_spec);
    if (js_object_type == NULL)
    {
        return -1;
    }
    return 0;
}


js_object *
js_object_new(Local<Object> object, Local<Context> context)
{
    Local<Context> creation;
    if (object->GetCreationContext().ToLocal(&creation) &&
        creation->GetNumberOfEmbedderDataFields() > CONTEXT_OBJECT_SLOT) {
        auto slot = creation->GetEmbedderData(CONTEXT_OBJECT_SLOT);
        if (slot->IsExternal() && !context_is_active(
                context_from_v8(creation))) {
            PyErr_SetString(PyExc_RuntimeError, "JavaScript object Context is closed");
            return nullptr;
        }
    }
    IsolateState* isolate_state = runtime_manager.find_isolate(v8_compat::context_isolate(context));
    IN_ISOLATE(isolate_state);
    Context::Scope cs(context);
    js_object *self;
    
    if (object->IsPromise()) {
        self = (js_object*)PyType_GenericAlloc(js_promise_type, 0);
    } else if (object->IsCallable()) {
        self = (js_object *)PyType_GenericAlloc(js_function_type, 0);
    } else if (object->IsTypedArray()) {
        self = (js_object*)PyType_GenericAlloc(js_typedarray_type, 0);
    } else {
        self = (js_object*)PyType_GenericAlloc(js_object_type, 0);
    }

    if (self != NULL) {
        js_object_set_persistent(self, object, context, true);
    }
    return self;
}

void 
js_object_weak_callback(const WeakCallbackInfo<js_object> &info)
{
    js_object *self = info.GetParameter();
    self->object.Reset();
    self->weak_owned = false;
    self->isolate_state->defer_release(&self->deferred_release, (PyObject*)self);
}

js_object *
js_object_weak_new(Local<Object> object, Local<Context> context)
{
    IsolateState* isolate_state = runtime_manager.find_isolate(v8_compat::context_isolate(context));
    IN_ISOLATE(isolate_state);
    Context::Scope cs(context);
    js_object *self;
    if (object->IsPromise()) {
        self = (js_object*)PyType_GenericAlloc(js_promise_type, 0);
    }
    else if (object->IsCallable()) {
        self = (js_object*)PyType_GenericAlloc(js_function_type, 0);
    }
    else if (object->IsTypedArray()) {
        self = (js_object*)PyType_GenericAlloc(js_typedarray_type, 0);
    }
    else {
        self = (js_object*)PyType_GenericAlloc(js_object_type, 0);
    }

    if (self != NULL) {
        js_object_set_persistent(self, object, context, false);
        // The V8 weak callback owns this reference and releases it on GC.
        Py_INCREF((PyObject*)self);
        self->weak_owned = true;
        self->object.persistent().SetWeak(
            self, js_object_weak_callback, WeakCallbackType::kParameter);
    }
    return self;
}

// Python's own lookup, __getattr__ included.  NULL with no error means Python has
// nothing -- the caller's cue to ask JavaScript -- and NULL with an error means
// Python itself failed.
static PyObject*
js_object_python_attribute(js_object* self, PyObject* name)
{
    PyObject* value = PyObject_GenericGetAttr((PyObject*)self, name);
    if (value != nullptr)
        return value;
    if (!PyErr_ExceptionMatches(PyExc_AttributeError))
        return nullptr;
    PyErr_Clear();
    PyObject* hook = PyObject_GetAttrString((PyObject*)Py_TYPE((PyObject*)self), "__getattr__");
    if (hook == nullptr) {
        if (!PyErr_ExceptionMatches(PyExc_AttributeError))
            return nullptr;
        PyErr_Clear();
        return nullptr;
    }
    // A plain function on the type, so the receiver goes with it; a bound method
    // already carries one.
    PyObject* result = PyMethod_Check_abi3(hook)
        ? PyObject_CallFunctionObjArgs(hook, name, NULL)
        : PyObject_CallFunctionObjArgs(hook, self, name, NULL);
    Py_DECREF(hook);
    return result;
}

// obj.name and obj["name"] are one lookup here but two different failures.
enum class MissingProperty { Attribute, Key };

static PyObject*
js_object_lookup(js_object* self, PyObject* name, MissingProperty missing)
{
    if (js_object_should_try_python_attribute(self, name))
    {
        PyObject* attr_value = js_object_python_attribute(self, name);
        if (attr_value != nullptr)
            return attr_value;
        if (PyErr_Occurred())
            return nullptr;
    }

    if (!js_object_require_active(self))
        return NULL;
    ExecutionScope execution((context_c*)self->registration.context, (PyObject*)self);
    if (!execution || !js_object_require_active(self)) return nullptr;
    auto* isolate = execution.isolate();
    Local<Object> object = self->object.Get(isolate);
    IN_CONTEXT(object->GetCreationContextChecked());
    Local<Value> js_name = js_from_py(name, context);
    if (PyErr_Occurred()) return NULL;
    if (js_name.IsEmpty()) {
        PyErr_SetString(PyExc_RuntimeError, "failed to convert JavaScript property name");
        return NULL;
    }
    JS_TRY
    DeadlineScope timeout_guard(context);
    timeout_guard.start();
    Maybe<bool> maybe_has_property = without_gil([&] { return object->Has(context, js_name); });
    timeout_guard.stop();
    PY_PROPAGATE_JS;
    bool has_property;
    if (!maybe_has_property.To(&has_property)) {
        PY_PROPAGATE_JS;
        PyErr_SetString(PyExc_RuntimeError, "failed to check JavaScript property");
        return NULL;
    }
    if (!has_property) {
        // A key error carries the key; an attribute error names the object, and
        // says less when the engine knows no constructor name.
        if (missing == MissingProperty::Key) {
            PyErr_SetObject(PyExc_KeyError, name);
            return NULL;
        }
        Local<String> constructor_name = object->GetConstructorName();
        if (constructor_name.IsEmpty()) {
            PyErr_Format(PyExc_AttributeError,
                "JavaScript object has no attribute '%.400S'", name);
            return NULL;
        }
        PyObject *class_name = py_from_js(constructor_name, context);
        PyErr_PROPAGATE(class_name);
        PyErr_Format(PyExc_AttributeError, "'%.50S' JavaScript object has no attribute '%.400S'",
                class_name, name);
        Py_XDECREF((PyObject*)class_name);
        return NULL;
    }

    timeout_guard.start();
    MaybeLocal<Value> js_value = without_gil([&] { return object->Get(context, js_name); });
    timeout_guard.stop();

    PY_PROPAGATE_JS;
    Local<Value> local_js_value;
    if (!js_value.ToLocal(&local_js_value)) {
        PY_PROPAGATE_JS;
        PyErr_SetString(PyExc_RuntimeError, "failed to get JavaScript property");
        return NULL;
    }
    PyObject *value = py_from_js(local_js_value, context);
    PyErr_PROPAGATE(value);
    // if this was called like object.method() then bind the return value to make it callable
    if (Py_TYPE((PyObject*)value) == js_function_type) {
        js_function *func = (js_function *) value;

        func->js_this.Set(func->base.isolate_state, isolate, object);
    }
    return value;
}

PyObject *
js_object_getattro(js_object *self, PyObject *name)
{
    return js_object_lookup(self, name, MissingProperty::Attribute);
}

static PyObject*
js_object_getitem(js_object* self, PyObject* key)
{
    return js_object_lookup(self, key, MissingProperty::Key);
}

int 
js_object_setattro(js_object *self, PyObject *name, PyObject *value)
{
    if (js_object_should_try_python_attribute(self, name))
    {
        PyObject *attr_value = PyObject_GenericGetAttr((PyObject *)self, name);
        if (attr_value == NULL)
        {
            // Python owns a name it already has.  A lookup that failed for any
            // other reason is a failure, not an absence, and writing to
            // JavaScript instead would hide it.
            if (!PyErr_ExceptionMatches(PyExc_AttributeError))
                return -1;
            PyErr_Clear();
        }
        else
        {
            Py_XDECREF((PyObject*)attr_value);
            return PyObject_GenericSetAttr((PyObject *)self, name, value);
        }
    }

    if (!js_object_require_active(self))
        return -1;
    ExecutionScope execution((context_c*)self->registration.context, (PyObject*)self);
    if (!execution || !js_object_require_active(self)) return -1;
    auto* isolate = execution.isolate();
    Local<Object> object = self->object.Get(isolate);
    IN_CONTEXT(object->GetCreationContextChecked());
    JS_TRY
    Local<Value> js_name = js_from_py(name, context);
    if (PyErr_Occurred()) return -1;
    Local<Value> js_value;
    if (value != NULL) {
        js_value = js_from_py(value, context);
        if (PyErr_Occurred()) return -1;
    }

    DeadlineScope timeout_guard(context);
    timeout_guard.start();

    Maybe<bool> maybe_worked = without_gil([&] {
        return value != NULL
            ? object->Set(context, js_name, js_value)
            : object->Delete(context, js_name);
    });

    timeout_guard.stop();

    PY_PROPAGATE_JS_RET(-1);
    bool worked;
    if (!maybe_worked.To(&worked)) {
        PY_PROPAGATE_JS_RET(-1);
        PyErr_SetString(PyExc_RuntimeError, "failed to set JavaScript property");
        return -1;
    }
    return 0;
}

PyObject *
js_object_getiter(js_object *self)
{
    PyObject *props = js_object_dir(self);
    PyErr_PROPAGATE(props);
    PyObject *iter = PySeqIter_New(props);
    Py_XDECREF((PyObject*)props);
    return iter;
}

PyObject *
js_object_dir(js_object *self)
{
    if (!js_object_require_active(self))
        return NULL;
    ExecutionScope execution((context_c*)self->registration.context, (PyObject*)self);
    if (!execution || !js_object_require_active(self)) return nullptr;
    auto* isolate = execution.isolate();
    Local<Object> object = self->object.Get(isolate);
    Local<Context> context = object->GetCreationContextChecked();
    Context::Scope cs(context);
    JS_TRY

    DeadlineScope timeout_guard(context);
    timeout_guard.start();
    MaybeLocal<Array> maybe_properties = without_gil([&] { return object->GetOwnPropertyNames(context, ALL_PROPERTIES); });
    timeout_guard.stop();
    PY_PROPAGATE_JS;
    Local<Array> properties;
    if (!maybe_properties.ToLocal(&properties)) {
        PY_PROPAGATE_JS;
        PyErr_SetString(PyExc_RuntimeError, "failed to get JavaScript property names");
        return NULL;
    }
    PyObject *py_properties = PyList_New(properties->Length());
    PyErr_PROPAGATE(py_properties);

    timeout_guard.start();
    for (unsigned i = 0; i < properties->Length(); i++) {
        MaybeLocal<Value> js_property = properties->Get(context, i);
        Local<Value> local_js_property;
        if (!js_property.ToLocal(&local_js_property)) {
            timeout_guard.stop();
            Py_DECREF((PyObject*)py_properties);
            PY_PROPAGATE_JS;
            PyErr_SetString(PyExc_RuntimeError, "failed to get JavaScript property name");
            return NULL;
        }
        PyObject *py_property = py_from_js(local_js_property, context);
        if (py_property == NULL) {
            timeout_guard.stop();
            Py_DECREF((PyObject*)py_properties);
            return NULL;
        }
        if (PyList_SetItem(py_properties, i, py_property) < 0) {
            timeout_guard.stop();
            Py_DECREF((PyObject*)py_property);
            Py_DECREF((PyObject*)py_properties);
            return NULL;
        }
    }

    timeout_guard.stop();

    return py_properties;
}

PyObject *
js_object_richcompare(js_object* self, PyObject* other, int operation)
{
    if (!PyObject_TypeCheck(other, js_object_type)) {
        PYV8_RETURN_NOTIMPLEMENTED;
    }

    if (operation != Py_EQ && operation != Py_NE) {
        PYV8_RETURN_NOTIMPLEMENTED;
    }

    js_object* other_object = (js_object*)other;
    if (!js_object_require_active(self) || !js_object_require_active(other_object)) {
        return NULL;
    }
    if (self->isolate_state != other_object->isolate_state) {
        if (operation == Py_NE)
            PYV8_RETURN_TRUE;
        PYV8_RETURN_FALSE;
    }
    ExecutionScope execution((context_c*)self->registration.context, (PyObject*)self);
    if (!execution || !js_object_require_active(self)) return nullptr;
    auto* isolate = execution.isolate();
    JS_TRY;

    if (!js_object_require_active(other_object)) return nullptr;

    Local<Object> js_obj = self->object.Get(isolate);
    Local<Object> js_obj_other = other_object->object.Get(isolate);
    bool equals = !js_obj_other.IsEmpty() && !js_obj.IsEmpty() &&
                  js_obj->StrictEquals(js_obj_other);
    if (operation == Py_NE)
        equals = !equals;
    return PyBool_FromLong(equals ? 1 : 0);
}

PyObject *
js_object_repr(js_object *self)
{
    if (!js_object_require_active(self))
        return NULL;
    ExecutionScope execution((context_c*)self->registration.context, (PyObject*)self);
    if (!execution || !js_object_require_active(self)) return nullptr;
    auto* isolate = execution.isolate();
    Local<Object> object = self->object.Get(isolate);
    IN_CONTEXT(object->GetCreationContextChecked());
    JS_TRY;
    DeadlineScope timeout_guard(context);
    timeout_guard.start();
    Local<String> v8_str;
    MaybeLocal<String> maybe_v8_str = without_gil([&] { return object->ToString(context); });
    timeout_guard.stop();
    if (!maybe_v8_str.ToLocal(&v8_str)) {
        PY_PROPAGATE_JS;
        PyErr_SetString(PyExc_RuntimeError, "JavaScript ToString failed");
        return nullptr;
    }

    v8::String::Utf8Value str(isolate, v8_str);
    PyObject* repr = PyUnicode_DecodeUTF8(*str, str.length(), NULL);
    PY_PROPAGATE_JS;
    return repr;
}

int js_object_traverse(js_object* self, visitproc visit, void* arg) {
    Py_VISIT(self->py_context);
    Py_VISIT(self->py_isolate);
    return 0;
}

int js_object_clear(js_object* self)
{
    auto* state = self->isolate_state;
    IsolateCleanupScope cleanup(state, (PyObject*)self, (int (*)(PyObject*))js_object_clear);
    if (!cleanup.ready()) return 0;
    self->registration.detach();
    if (PyObject_TypeCheck((PyObject*)self, js_function_type))
        ((js_function*)self)->js_this.Reset();
    self->object.Reset();
    if (self->weak_owned) {
        self->weak_owned = false;
        state->defer_release(&self->deferred_release, (PyObject*)self);
    }
    self->isolate_state = nullptr;
    Py_CLEAR(self->py_context);
    Py_CLEAR(self->py_isolate);
    return 0;
}

void js_object_dealloc(js_object *self)
{
    PyObject_GC_UnTrack(self);
    js_object_clear(self);
    if (Py_REFCNT((PyObject*)self) != 0) return;
    TP_FREE_ABI3(js_object_type, self);
}
