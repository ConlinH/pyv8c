#include "bridge_base.h"
#include <v8.h>
#include <Python.h>
#include <climits>
#include <sstream>

#include "crossing.h"
#include "convert.h"
#include "pyclass.h"
#include "idl_spec.h"
#include "pyclass_handlers.h"
#include "trace.h"

using namespace v8;

enum FlagConstructor {
    kNew = 1 << 0,
    kCall = 1 << 1,
};

// The keyword a constructor is offered its window under, interned once and handed
// to the call as a key-name tuple rather than as a dictionary.
static PyObject* constructor_window_keyword()
{
    static PyObject* cached = nullptr;
    if (cached == nullptr)
    {
        PyObject* name = PyUnicode_InternFromString("win");
        if (name == nullptr)
            return nullptr;
        cached = PyTuple_Pack(1, name);
        Py_DECREF((PyObject*)name);
    }
    return cached;
}

// The Web IDL receiver, resolved in its own realm: a member installed on one realm's
// template is also reached through another realm's WindowProxy.
static PyObject* resolve_receiver(const FunctionCallbackInfo<Value>& info)
{
    Local<Object> js_self = info.This();
    Local<Context> receiver_context;
    if (!js_self->GetCreationContext().ToLocal(&receiver_context))
        return Py_None;
    if (js_self->Equals(receiver_context, receiver_context->Global()).FromMaybe(false))
        js_self = v8_compat::global_template_object(receiver_context);
    if (js_self->InternalFieldCount() != OBJECT_INTERNAL_FIELDS)
        return Py_None;
    IsolateState* isolate_state = runtime_manager.find_isolate(info.GetIsolate());
    if (isolate_state == nullptr ||
        js_self->GetInternalField(PYTHON_MAGIC_FIELD) !=
            isolate_state->v8_string(V8StringId::PythonObjectMagic))
        return Py_None;
    return IsolateState::python_object(js_self);
}

// An object read through this receiver belongs to the receiver's realm.
static Local<Context> receiver_realm(const FunctionCallbackInfo<Value>& info,
                                     Local<Context> fallback)
{
    Local<Context> realm;
    return info.This()->GetCreationContext().ToLocal(&realm) ? realm : fallback;
}

static void
py_class_construct_impl(const FunctionCallbackInfo<Value>& info,
                        Local<Context> context,
                        py_class* self,
                        context_c* py_context)
{
    Isolate* isolate = info.GetIsolate();
    bool can_new = (self->constructor_behavior & FlagConstructor::kNew) != 0;
    bool can_call = (self->constructor_behavior & FlagConstructor::kCall) != 0;
    if (!can_call && !can_new) {
        info.GetIsolate()->ThrowException(Exception::TypeError(JSTR("Illegal constructor")));
        return;
    }
    if (!can_call && !info.IsConstructCall()) {
        std::ostringstream message;
        message << "Failed to construct '" << PyUnicode_AsUTF8_abi3(self->cls_name)
                << "': Please use the 'new' operator, this DOM object constructor "
                   "cannot be called as a function.";
        info.GetIsolate()->ThrowException(Exception::TypeError(JSTR(message.str().c_str())));
        return;
    }
    if (self->unconstructable) {
        PyObject* message = PyUnicode_FromFormat("%S is not a constructor", self->cls_name);
        if (message == NULL) {
            js_throw_py();
            return;
        }
        Local<Value> js_message = js_from_py(message, context);
        Py_DECREF((PyObject*)message);
        if (js_message.IsEmpty() || PyErr_Occurred()) {
            js_throw_py();
            return;
        }
        info.GetIsolate()->ThrowException(Exception::TypeError(js_message.As<String>()));
        return;
    }

    // The arguments travel as a vectorcall array: the JavaScript ones, then the
    // window under the one keyword a constructor is offered.  A tuple and a
    // keyword dictionary per `new Something()` were two allocations for an
    // argument list that is almost always empty.
    const int argument_count = info.Length();
    const int keyword_count = py_context->py_global != nullptr ? 1 : 0;
    PyObject* inline_arguments[8] = {};
    std::unique_ptr<PyObject*[]> heap_arguments;
    PyObject** arguments = inline_arguments;
    const int capacity = argument_count + keyword_count;
    if (capacity > (int)(sizeof(inline_arguments) / sizeof(inline_arguments[0]))) {
        heap_arguments.reset(new (std::nothrow) PyObject*[capacity]());
        if (heap_arguments == nullptr) {
            PyErr_NoMemory();
            js_throw_py();
            return;
        }
        arguments = heap_arguments.get();
    }
    // *owned* counts the arguments this function holds a reference to: the
    // converted JavaScript ones.  The window travels borrowed, so it is never in
    // that count and never decref'd.
    int owned = 0;
    int filled = 0;
    for (int index = 0; index < argument_count; ++index) {
        PyObject* argument = py_from_js(info[index], context);
        if (argument == nullptr) {
            while (owned > 0) Py_DECREF(arguments[--owned]);
            js_throw_py();
            return;
        }
        arguments[filled++] = argument;
        owned++;
    }
    PyObject* keywords = nullptr;
    if (keyword_count != 0) {
        arguments[filled++] = py_context->py_global;
        keywords = constructor_window_keyword();
        if (keywords == nullptr) {
            while (owned > 0) Py_DECREF(arguments[--owned]);
            js_throw_py();
            return;
        }
    }
    PyObject* new_object = nullptr;
    if (self->constructor_adapter != NULL) {
        // An adapter takes the class, the arguments and the keywords as three
        // objects, so this path still has to build them.
        PyObject* args = PyTuple_New(owned);
        if (args == nullptr) {
            while (owned > 0) Py_DECREF(arguments[--owned]);
            js_throw_py();
            return;
        }
        for (int index = 0; index < owned; ++index) {
            Py_INCREF(arguments[index]);
            PyTuple_SetItem(args, index, arguments[index]);
        }
        PyObject* kwargs = PyDict_New();
        if (kwargs == NULL) {
            Py_DECREF((PyObject*)args);
            while (owned > 0) Py_DECREF(arguments[--owned]);
            js_throw_py();
            return;
        }
        if (keyword_count != 0 &&
            PyDict_SetItem(kwargs, PyTuple_GetItem(keywords, 0), py_context->py_global) < 0) {
            Py_DECREF((PyObject*)args);
            Py_DECREF((PyObject*)kwargs);
            while (owned > 0) Py_DECREF(arguments[--owned]);
            js_throw_py();
            return;
        }
        new_object = PyObject_CallFunctionObjArgs(
            self->constructor_adapter, self->cls, args, kwargs, NULL);
        Py_DECREF((PyObject*)args);
        Py_DECREF((PyObject*)kwargs);
    }
    else {
        // The count is of positional arguments: the keyword's value is the entry
        // after them, which is where the call reads it from.
        new_object = PyObject_Vectorcall(
            self->cls, arguments, (size_t)owned, keywords);
    }
    while (owned > 0) Py_DECREF(arguments[--owned]);
    if (new_object == NULL) {
        js_throw_py();
        return;
    }
    if (!py_class_init_js_object(
            v8_compat::callback_holder(info), new_object, context,
            py_class_shares_wrapper(self))) {
        Py_DECREF((PyObject*)new_object);
        js_throw_py();
        return;
    }
    Py_DECREF((PyObject*)new_object);
}


void py_class_construct_callback(const FunctionCallbackInfo<Value>& info) {
    SETUP;
    if (!context_check_callback(context)) return;

    py_class *self = (py_class *) IsolateState::callback_target(info.Data());
    if (self == nullptr) { js_throw_py(); return; }
    context_c* ctx_c = context_from_v8(context);
    if (ctx_c == nullptr)
        return;
    if (context_trace_enabled(ctx_c)) {
        {
            trace::TraceScope construct_trace(info, 3, self->cls_name, self->cls, ctx_c);
            py_class_construct_impl(info, context, self, ctx_c);
        }
        if (PyErr_Occurred())
            js_throw_py();
    }
    else {
        py_class_construct_impl(info, context, self, ctx_c);
    }
}

static void py_class_method_impl(const FunctionCallbackInfo<Value> &info, Local<Context> context,
                                 PyObject *receiver, MethodSpec *binding)
{
    PyOwnedRef callable(PyV8_NewRef(binding->callback));
    // An unchecked operation still receives a receiver slot when this is not a
    // host wrapper. Dropping that slot shifts its first argument into self.
    int offset = binding->pass_receiver ? 1 : 0;
    PythonArguments args(info.Length() + offset);
    if (!args.valid() || !args.convert(info, context, offset ? receiver : nullptr)) {
        js_throw_py(); return;
    }
    PyObject *result = PyObject_Vectorcall(callable.get(), args.data(), args.size(), nullptr);
    if (result == nullptr)
    {
        js_throw_py();
        return;
    }
    Local<Value> converted = js_from_py(result, receiver_realm(info, context));
    Py_DECREF((PyObject*)result);
    if (converted.IsEmpty() || PyErr_Occurred())
    {
        js_throw_py();
        return;
    }
    info.GetReturnValue().Set(converted);
}

void py_class_method_callback(const FunctionCallbackInfo<Value> &info)
{
    SETUP;
    if (!context_check_callback(context)) return;
    auto *binding = (MethodSpec *)IsolateState::callback_target(info.Data());
    if (binding == nullptr) { js_throw_py(); return; }
    PyObject *receiver = binding->pass_receiver ? resolve_receiver(info) : Py_None;
    if (receiver == nullptr)
    {
        js_throw_py();
        return;
    }
    // A kCheck member was already filtered by its V8 signature; the receiver here is
    // whatever that signature accepted, as in Blink.
    context_c *owner = context_from_v8(context);
    if (owner == nullptr)
        return;
    if (context_trace_enabled(owner))
    {
        PyObject *trace_receiver = receiver;
        if (trace_receiver == Py_None && binding->trace_receiver)
            trace_receiver = binding->trace_receiver;
        const bool named = trace_receiver == Py_None;
        if (named)
            trace_receiver = trace_interface_name(info);
        {
            trace::TraceScope operation_trace(info, 2, binding->name, trace_receiver, owner);
            py_class_method_impl(info, context, receiver, binding);
        }
        if (named)
            Py_XDECREF(trace_receiver);
        if (PyErr_Occurred())
            js_throw_py();
    }
    else
    {
        py_class_method_impl(info, context, receiver, binding);
    }
}
// --- Interceptors ---
static void attribute_get_impl(const FunctionCallbackInfo<Value>& info, Local<Context> context) {
    auto* binding = (AttributeSpec*)IsolateState::callback_target(info.Data());
    if (binding == nullptr) { js_throw_py(); return; }
    PyObject* self = binding->getter_receiver ? resolve_receiver(info) : Py_None;
    if (!self) { js_throw_py(); return; }

    PyOwnedRef keep_self(PyV8_NewRef(self));
    // No reference is taken on binding: nothing reads it after the call below,
    // and the callback it runs may close the context, which is what releases it.
    PyObject* argv[] = {self};
    PyObject* result = PyObject_Vectorcall(binding->getter, argv, self == Py_None ? 0 : 1, nullptr);
    JS_PROPAGATE_PY(result);
    auto value = js_from_py(result, receiver_realm(info, context));
    Py_DECREF((PyObject*)result);
    if (value.IsEmpty() || PyErr_Occurred()) { js_throw_py(); return; }
    info.GetReturnValue().Set(value);
}

void py_attribute_for_get(const FunctionCallbackInfo<Value>& info) {
    SETUP;
    if (!context_check_callback(context)) return;
    auto* owner = context_from_v8(context);
    if (!context_trace_enabled(owner)) { attribute_get_impl(info, context); return; }
    auto* binding = (AttributeSpec*)IsolateState::callback_target(info.Data());
    if (binding == nullptr) { js_throw_py(); return; }
    PyObject* receiver = binding->getter_receiver ? resolve_receiver(info) : Py_None;
    const bool named = receiver == Py_None;
    if (named)
        receiver = trace_interface_name(info);
    trace::TraceScope observation(info, 0, binding->name, receiver, owner);
    if (named)
        Py_XDECREF(receiver);
    attribute_get_impl(info, context);
}

static void attribute_set_impl(const FunctionCallbackInfo<Value>& info, Local<Context> context,
                               bool instance) {
    auto* isolate = info.GetIsolate();
    if (info.Length() < 1) {
        isolate->ThrowException(v8::Exception::TypeError(V8String(isolate, "1 argument required, but only 0 present")));
        return;
    }
    auto* binding = (AttributeSpec*)IsolateState::callback_target(info.Data());
    if (binding == nullptr) { js_throw_py(); return; }
    PyObject* self = binding->setter_receiver ? resolve_receiver(info) : Py_None;
    if (!self) { js_throw_py(); return; }

    PyOwnedRef keep_self(PyV8_NewRef(self));
    // This one is load-bearing: replaceable is read off binding *after* the
    // setter runs, and a setter that closes its context drops the reference the
    // context held on the spec.
    PyOwnedRef keep_binding(PyV8_NewRef((PyObject*)binding));
    PyObject* value = py_from_js(info[0], context);
    JS_PROPAGATE_PY(value);
    PyObject* argv[] = {self, value};
    PyObject* result = PyObject_Vectorcall(binding->setter, argv + (self == Py_None),
                                         self == Py_None ? 1 : 2, nullptr);
    Py_DECREF((PyObject*)value);
    JS_PROPAGATE_PY(result);
    Py_DECREF((PyObject*)result);
    if (instance && binding->replaceable) {
        const char* name = PyUnicode_AsUTF8_abi3(binding->name);
        if (name == nullptr) { js_throw_py(); return; }
        auto replaced = info.This()->CreateDataProperty(context, V8String(isolate, name), info[0]);
        if (replaced.IsNothing()) return;
    }
}

static void attribute_set(const FunctionCallbackInfo<Value>& info, bool instance) {
    SETUP;
    if (!context_check_callback(context)) return;
    auto* owner = context_from_v8(context);
    if (!context_trace_enabled(owner)) { attribute_set_impl(info, context, instance); return; }
    auto* binding = (AttributeSpec*)IsolateState::callback_target(info.Data());
    if (binding == nullptr) { js_throw_py(); return; }
    PyObject* receiver = binding->setter_receiver ? resolve_receiver(info) : Py_None;
    const bool named = receiver == Py_None;
    if (named)
        receiver = trace_interface_name(info);
    trace::TraceScope observation(info, 1, binding->name, receiver, owner);
    if (named)
        Py_XDECREF(receiver);
    attribute_set_impl(info, context, instance);
}

void py_attribute_for_instance_set(const FunctionCallbackInfo<Value>& info) { attribute_set(info, true); }
void py_attribute_for_property_set(const FunctionCallbackInfo<Value>& info) { attribute_set(info, false); }
