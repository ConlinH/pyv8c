#include "bridge_base.h"
#include "jsnull.h"
#include "jsundefined.h"
#include <Python.h>
#include "entry.h"
#include "runtime.h"
#include <v8.h>

#include "convert.h"
#include "pyfunction.h"
#include "pyclass.h"
#include "jsobject.h"
#include "jsvalue.h"
#include "context.h"

#include <climits>
#include <memory>
#include <new>
#include <unordered_map>

using namespace v8;

namespace
{
class PyToJsConversionContext
{
public:
    explicit PyToJsConversionContext(Isolate* isolate)
        : inline_size_(0), isolate_(isolate)
    {
    }

    bool find(PyObject* object, Local<Value>* result) const
    {
        for (size_t i = 0; i < inline_size_; ++i)
        {
            if (inline_objects_[i] == object)
            {
                *result = inline_values_[i];
                return true;
            }
        }
        auto item = overflow_.find(object);
        if (item != overflow_.end())
        {
            *result = item->second.Get(isolate_);
            return true;
        }
        return false;
    }

    bool remember(PyObject* object, Local<Value> result)
    {
        if (inline_size_ < kInlineCapacity)
        {
            inline_objects_[inline_size_] = object;
            inline_values_[inline_size_] = result;
            ++inline_size_;
            return true;
        }
        try
        {
            overflow_.emplace(object, Global<Value>(isolate_, result));
        }
        catch (const std::bad_alloc&)
        {
            PyErr_NoMemory();
            return false;
        }
        return true;
    }

private:
    static constexpr size_t kInlineCapacity = 16;
    size_t inline_size_;
    PyObject* inline_objects_[kInlineCapacity];
    Local<Value> inline_values_[kInlineCapacity];
    std::unordered_map<PyObject*, Global<Value>> overflow_;
    Isolate* isolate_;
};
}

static Local<Value>
js_from_py_impl(PyObject *value,
                 Local<Context> context,
                 PyToJsConversionContext* conversion,
                 Isolate* isolate)
{
    if (value == Py_False)
    {
        return False(isolate);
    }
    if (value == Py_True)
    {
        return True(isolate);
    }
    if (value == js_undefined_object)
    {
        return Undefined(isolate);
    }
    if (value == Py_None)
    {
        return Undefined(isolate);
    }
    if (value == js_null_object)
    {
        return Null(isolate);
    }
    if (PyUnicode_Check(value))
    {
        Py_ssize_t len;
        const char *str = PyUnicode_AsUTF8AndSize(value, &len);
        if (str == NULL)
        {
            if (!PyErr_ExceptionMatches(PyExc_UnicodeEncodeError))
                return Local<Value>();
            // UTF-8 rejects unpaired surrogates; JavaScript accepts the
            // corresponding UTF-16 code units as part of a string.
            PyErr_Clear();
            PyOwnedRef encoded(PyUnicode_AsEncodedString(value, "utf-16-le", "surrogatepass"));
            if (!encoded.get())
                return Local<Value>();
            char* data;
            Py_ssize_t bytes;
            if (PyBytes_AsStringAndSize(encoded.get(), &data, &bytes) < 0)
                return Local<Value>();
            if (bytes / 2 > INT_MAX)
            {
                PyErr_SetString(PyExc_OverflowError, "Python string is too large for a V8 string");
                return Local<Value>();
            }
            const int length = static_cast<int>(bytes / 2);
            std::unique_ptr<uint16_t[]> units(new (std::nothrow) uint16_t[length]);
            if (!units)
            {
                PyErr_NoMemory();
                return Local<Value>();
            }
            for (int index = 0; index < length; ++index)
            {
                const auto low = static_cast<unsigned char>(*data++);
                const auto high = static_cast<unsigned char>(*data++);
                units[index] = static_cast<uint16_t>(low | (high << 8));
            }
            Local<String> text;
            if (!String::NewFromTwoByte(isolate, units.get(), NewStringType::kNormal, length).ToLocal(&text))
            {
                PyErr_SetString(PyExc_RuntimeError, "failed to create JavaScript string");
                return Local<Value>();
            }
            return text;
        }
        if (len > INT_MAX)
        {
            PyErr_SetString(PyExc_OverflowError,
                            "Python string is too large for a V8 string");
            return Local<Value>();
        }
        const int length = static_cast<int>(len);
        // ASCII is what a page hands over most of the time, and UTF-8 spells it
        // one byte per code unit: V8 can take those bytes as they are.  The scan
        // is over the buffer CPython already cached and costs less than the decode.
        bool ascii = true;
        for (Py_ssize_t index = 0; index < len; ++index)
        {
            if ((static_cast<unsigned char>(str[index]) & 0x80) != 0)
            {
                ascii = false;
                break;
            }
        }
        Local<String> js_value;
        if (ascii)
        {
            if (!String::NewFromOneByte(
                    isolate, reinterpret_cast<const uint8_t*>(str),
                    NewStringType::kNormal, length).ToLocal(&js_value))
            {
                PyErr_SetString(PyExc_RuntimeError, "failed to create JavaScript string");
                return Local<Value>();
            }
            return js_value;
        }
        if (!String::NewFromUtf8(
                isolate, str, NewStringType::kNormal,
                length).ToLocal(&js_value))
        {
            PyErr_SetString(PyExc_RuntimeError, "failed to create JavaScript string");
            return Local<Value>();
        }
        return js_value;
    }
    if (PyBytes_Check(value))
    {
        char *str;
        Py_ssize_t len;
        if (PyBytes_AsStringAndSize(value, &str, &len) < 0)
        {
            return Undefined(isolate);
        }
        Local<ArrayBuffer> js_value = ArrayBuffer::New(isolate, static_cast<size_t>(len));
        if (len > 0)
        {
            memcpy(js_value->Data(), str, static_cast<size_t>(len));
        }
        return js_value;
    }

    if (PyNumber_Check(value))
    {
        Local<Number> js_value;
        if (PyFloat_Check(value))
        {
            js_value = Number::New(isolate, PyFloat_AsDouble(value));
        }
        else if (PyLong_Check(value))
        {
            long long long_value = PyLong_AsLongLong(value);
            if (!PyErr_Occurred() &&
                long_value >= INT32_MIN && long_value <= INT32_MAX)
            {
                js_value = Integer::New(isolate, static_cast<int32_t>(long_value));
            }
            else
            {
                // JavaScript numbers can represent integers larger than Int32,
                // but Python conversion must still report values that cannot be
                // converted to a finite JavaScript Number.
                PyErr_Clear();
                double double_value = PyLong_AsDouble(value);
                if (PyErr_Occurred())
                    return Local<Value>();
                js_value = Number::New(isolate, double_value);
            }
        }
        else
        {
            // A Python number that is neither an int nor a float: Decimal and
            // Fraction land here.  JavaScript has no number to give them, and an
            // undefined that says nothing is worse than an error that says why.
            PyObject* kind = PyType_GetName(Py_TYPE(value));
            if (kind != nullptr) {
                PyErr_Format(PyExc_TypeError,
                             "cannot convert a Python %U to a JavaScript number", kind);
                Py_DECREF(kind);
            } else {
                PyErr_SetString(PyExc_TypeError,
                                "cannot convert a Python number to a JavaScript number");
            }
            return Local<Value>();
        }
        return js_value;
    }

    if (PyDict_Check(value))
    {
        PyToJsConversionContext local_conversion(isolate);
        if (conversion == nullptr)
            conversion = &local_conversion;

        Local<Value> existing;
        if (conversion->find(value, &existing))
            return existing;

        // a context scope is (I think) needed for Object::New to work
        Context::Scope cs(context);
        Local<Object> js_dict = Object::New(isolate);
        if (!conversion->remember(value, js_dict))
            return Local<Value>();

        PyObject *dict = value;
        PyObject *key, *dict_value;
        Py_ssize_t pos = 0;
        while (PyDict_Next(dict, &pos, &key, &dict_value))
        {
            Local<Value> js_key = js_from_py_impl(key, context, conversion, isolate);
            if (js_key.IsEmpty() || PyErr_Occurred())
            {
                return Local<Value>();
            }
            Local<Value> js_value = js_from_py_impl(dict_value, context, conversion, isolate);
            if (js_value.IsEmpty() || PyErr_Occurred())
            {
                return Local<Value>();
            }
            // A property key, as JavaScript would take one: names as they are, and
            // anything else -- an int key, say -- by its string form.
            Local<Name> js_name;
            if (js_key->IsName())
            {
                js_name = js_key.As<Name>();
            }
            else
            {
                Local<String> text;
                if (!js_key->ToString(context).ToLocal(&text))
                {
                    PyErr_SetString(PyExc_RuntimeError, "failed to convert JavaScript property key");
                    return Local<Value>();
                }
                js_name = text;
            }

            bool set_worked;
            // Define rather than assign, as for array items: assigning would route a
            // "__proto__" key through Object.prototype's accessor and hand the data
            // control of the object's prototype.  A "no" that threw nothing is a
            // failed write too.
            if (!js_dict->CreateDataProperty(context, js_name, js_value).To(&set_worked) ||
                !set_worked)
            {
                PyErr_SetString(PyExc_RuntimeError, "failed to set JavaScript object property");
                return Local<Value>();
            }
        }
        return js_dict;
    }

    if (PyList_Check(value) || PyTuple_Check(value))
    {
        PyToJsConversionContext local_conversion(isolate);
        if (conversion == nullptr)
            conversion = &local_conversion;

        Local<Value> existing;
        if (conversion->find(value, &existing))
            return existing;

        Py_ssize_t length = PySequence_Length(value);
        if (length < 0 || length > INT_MAX)
        {
            if (!PyErr_Occurred())
                PyErr_SetString(PyExc_OverflowError, "Python sequence is too large for a JavaScript array");
            return Local<Value>();
        }
        Local<Array> array = Array::New(isolate, static_cast<int>(length));
        if (!conversion->remember(value, array))
            return Local<Value>();

        for (Py_ssize_t i = 0; i < length; i++)
        {
            PyObject *item = PyList_Check(value)
                ? PyList_GetItem(value, i)
                : PyTuple_GetItem(value, i);
            if (item == NULL)
            {
                return Local<Value>();
            }
            Local<Value> js_item = js_from_py_impl(item, context, conversion, isolate);
            if (js_item.IsEmpty() || PyErr_Occurred())
                return Local<Value>();

            bool set_worked;
            // Define rather than assign: building an array must not run a setter a
            // page put on Array.prototype, and a "no" that threw nothing is a failure.
            if (!array->CreateDataProperty(context, static_cast<uint32_t>(i), js_item)
                     .To(&set_worked) ||
                !set_worked)
            {
                PyErr_SetString(PyExc_RuntimeError, "failed to set JavaScript array item");
                return Local<Value>();
            }
        }
        return array;
    }
    if (PySet_Check(value) || PyFrozenSet_Check(value))
    {
        PyToJsConversionContext local_conversion(isolate);
        if (conversion == nullptr)
            conversion = &local_conversion;

        Local<Value> existing;
        if (conversion->find(value, &existing))
            return existing;

        Local<Set> js_set = Set::New(isolate);
        if (!conversion->remember(value, js_set))
            return Local<Value>();

        PyObject* iterator = PyObject_GetIter(value);
        if (iterator == nullptr)
            return Local<Value>();
        while (PyObject* item = PyIter_Next(iterator))
        {
            Local<Value> js_item = js_from_py_impl(item, context, conversion, isolate);
            Py_DECREF(item);
            if (js_item.IsEmpty() || PyErr_Occurred())
            {
                Py_DECREF(iterator);
                return Local<Value>();
            }
            if (js_set->Add(context, js_item).IsEmpty())
            {
                Py_DECREF(iterator);
                PyErr_SetString(PyExc_RuntimeError, "failed to add JavaScript set item");
                return Local<Value>();
            }
        }
        Py_DECREF(iterator);
        if (PyErr_Occurred())
            return Local<Value>();
        return js_set;
    }
    if (PyFunction_Check_abi3(value) || PyMethod_Check_abi3(value))
    {
        auto* owner = context_from_v8(context);
        if (owner == nullptr) {
            PyErr_SetString(PyExc_RuntimeError, "V8 context is closed");
            return Local<Value>();
        }
        py_function *templ = (py_function *)py_function_to_template(value, owner);
        if (templ == NULL)
        {
            return Local<Value>();
        }
        PyOwnedRef keep_template((PyObject*)templ);
        Local<Function> function = py_template_to_function(templ, context);
        if (function.IsEmpty())
        {
            return Local<Value>();
        }
        return function;
    }

    if (PyType_Check(value))
    {
        context_c *ctx_c = context_from_v8(context);
        if (ctx_c == nullptr)
        {
            PyErr_SetString(PyExc_RuntimeError, "V8 context has no Python owner");
            return Local<Value>();
        }
        py_class *templ = (py_class *)py_class_to_template(value, ctx_c);
        if (templ == NULL)
        {
            return Local<Value>();
        }
        PyOwnedRef keep_template((PyObject*)templ);
        // A namespace comes back as an object and a callback interface as a function with no
        // prototype: py_class_get_constructor is what knows which, and it does it in place.
        Local<Value> interface_object =
            py_class_get_constructor(context, templ, (py_class *)templ->parent);
        if (interface_object.IsEmpty())
        {
            return Local<Value>();
        }
        return interface_object;
    }
    if (PyObject_TypeCheck(value, js_object_type))
    {
        js_object *py_value = (js_object *)value;
        if (!js_object_require_active(py_value)) return Local<Value>();
        if (py_value->isolate_state == nullptr || py_value->isolate_state->isolate() != isolate)
        {
            PyErr_SetString(PyExc_RuntimeError, "JavaScript value belongs to another isolate; use pyv8.transfer() to copy it");
            return Local<Value>();
        }
        if (!context_retain_dependency(context, py_value->py_context)) return Local<Value>();
        return py_value->object.Get(isolate);
    }

    if (PyObject_TypeCheck(value, js_value_type))
    {
        js_value* py_value = (js_value*)value;
        if (py_value->value.IsEmpty()) {
            PyErr_SetString(PyExc_RuntimeError, "V8 value context is closed");
            return Local<Value>();
        }
        if (py_value->isolate_state == nullptr || py_value->isolate_state->isolate() != isolate)
        {
            PyErr_SetString(PyExc_RuntimeError, "JavaScript value belongs to another isolate; use pyv8.transfer() to copy it");
            return Local<Value>();
        }
        if (!context_retain_dependency(context, py_value->py_context)) return Local<Value>();
        return py_value->value.Get(isolate);
    }

    // python global_this -> js globalThis
    context_c *ctx_c = context.IsEmpty() ? nullptr :
        context_from_v8(context);
    if (ctx_c == nullptr) {
        PyErr_SetString(PyExc_RuntimeError, "Python objects require a V8 context");
        return Local<Value>();
    }
    if (ctx_c->py_global != NULL && Py_TYPE((PyObject*)value) == Py_TYPE(ctx_c->py_global))
    {
        PyObject *ctx = PyObject_GetAttrString(value, "_ctx");
        if (ctx == NULL)
        {
            if (PyErr_Occurred())
                PyErr_Clear();
            return context->Global();
        }

        context_c* source_context = (context_c*)ctx;
        if (source_context->isolate_state == nullptr || source_context->isolate_state->isolate() != isolate)
        {
            Py_DECREF((PyObject*)ctx);
            PyErr_SetString(PyExc_RuntimeError, "JavaScript value belongs to another isolate; use pyv8.transfer() to copy it");
            return Local<Value>();
        }
        auto js_content = source_context->js_context.Get(isolate);
        Py_DECREF((PyObject*)ctx);
        return js_content->Global();
    }
    // it's an arbitrary object
    PyObject *type;
    type = (PyObject*)Py_TYPE((PyObject*)value);
    py_class *templ = (py_class *)py_class_to_template(type, ctx_c);
    if (templ == NULL)
    {
        return Local<Value>();
    }
    PyOwnedRef keep_template((PyObject*)templ);
    Local<Object> js_object = py_class_create_js_object(templ, value, context);
    if (js_object.IsEmpty())
    {
        return Local<Value>();
    }
    return js_object;
}

Local<Value>
js_from_py(PyObject *value, Local<Context> context)
{
    if (context.IsEmpty()) {
        PyErr_SetString(PyExc_RuntimeError, "an explicit isolate is required without a Context");
        return Local<Value>();
    }
    return js_from_py(value, context, v8_compat::context_isolate(context));
}

Local<Value>
js_from_py(PyObject *value, Local<Context> context, Isolate* target_isolate)
{
    if (!context.IsEmpty() && v8_compat::context_isolate(context) != target_isolate) {
        PyErr_SetString(PyExc_RuntimeError, "JavaScript value belongs to another isolate; use pyv8.transfer() to copy it");
        return Local<Value>();
    }
    if (auto* active = current_isolate_state(); active && active->isolate() == target_isolate)
        return js_from_py_impl(value, context, nullptr, target_isolate);
    IsolateState* isolate_state = runtime_manager.find_isolate(target_isolate);
    if (isolate_state == nullptr || !isolate_state->require_active())
        return Local<Value>();
    ESCAPING_IN_ISOLATE(isolate_state);
    Local<Value> result = js_from_py_impl(value, context, nullptr, isolate);
    if (result.IsEmpty())
        return result;
    return hs.Escape(result);
}

// js_args is an out parameter, expected to contain enough space
void jss_from_pys(PyObject *py_args, Local<Value> *js_args, Local<Context> context)
{
    const Py_ssize_t length = PyTuple_Size(py_args);
    for (Py_ssize_t i = 0; i < length; i++)
    {
        js_args[i] = js_from_py(PyTuple_GetItem(py_args, i), context);
    }
}
