#include "bridge_base.h"
#include "jsnull.h"
#include "jsundefined.h"
#include <Python.h>
#include "runtime.h"
#include <v8.h>

#include "convert.h"
#include "jsobject.h"
#include "jsvalue.h"
#include "context.h"

#include <climits>
#include <array>
#include <memory>
#include <new>
#include <unordered_map>
#include <vector>

using namespace v8;

namespace
{
// Strings up to this length are converted through a stack buffer rather than the
// one v8::String::Utf8Value allocates, which is what a short string crossing the
// boundary would otherwise pay for.
constexpr int kInlineStringBytes = 128;
}

bool PythonArguments::convert(const FunctionCallbackInfo<Value>& info,
                              Local<Context> context, PyObject* receiver) {
    size_t offset = receiver ? 1 : 0;
    if (receiver) { Py_INCREF((PyObject*)receiver); values_[0] = receiver; }
    for (int i = 0; i < info.Length(); ++i) {
        values_[i + offset] = py_from_js(info[i], context);
        if (!values_[i + offset]) return false;
    }
    return true;
}

namespace
{
class JsToPyConversionContext
{
public:
    PyObject* find(Local<Object> object)
    {
        for (size_t i = 0; i < inline_size_; ++i) {
            if (inline_[i].object == object) {
                Py_INCREF(inline_[i].result);
                return inline_[i].result;
            }
        }
        if (visited_.empty()) return nullptr;
        const int identity_hash = object->GetIdentityHash();
        auto bucket = visited_.find(identity_hash);
        if (bucket == visited_.end())
            return NULL;
        for (const Entry& entry : bucket->second)
        {
            if (entry.object == object)
            {
                Py_INCREF(entry.result);
                return entry.result;
            }
        }
        return NULL;
    }

    bool remember(Local<Object> object, PyObject* result)
    {
        if (inline_size_ < inline_.size()) {
            inline_[inline_size_++] = {object, result};
            return true;
        }
        try
        {
            visited_[object->GetIdentityHash()].emplace_back(object, result);
        }
        catch (const std::bad_alloc&)
        {
            PyErr_NoMemory();
            return false;
        }
        return true;
    }

private:
    struct Entry {
        Local<Object> object;
        PyObject* result;
    };

    // Most result graphs fit here. Larger graphs keep hashed overflow buckets;
    // Local<Object> equality checks physical identity, including across GC.
    // One context belongs to the entire materialization, not to each recursion.
    std::array<Entry, 16> inline_;
    size_t inline_size_ = 0;
    // V8 identity hashes make overflow lookups constant-time on average. Object identity
    // remains the final check because identity hashes are not unique.
    std::unordered_map<int, std::vector<Entry>> visited_;
};
}

static PyObject *
py_from_js_impl(Local<Value> value,
                Local<Context> context,
                JsToPyConversionContext* conversion)
{
    Isolate* isolate = v8_compat::context_isolate(context);
    IsolateState* isolate_state = runtime_manager.find_isolate(isolate);
    if (value->IsSymbol())
    {
        value = value.As<Symbol>()->Description(v8_compat::context_isolate(context));
    }
    if (value->IsNull())
    {
        Py_INCREF((PyObject*)js_null_object);
        return js_null_object;
    }
    if (value->IsUndefined())
    {
        Py_INCREF((PyObject*)js_undefined_object);
        return js_undefined_object;
    }
    if (value->IsBoolean())
    {
        if (value.As<Boolean>()->Value())
            PYV8_RETURN_TRUE;
        PYV8_RETURN_FALSE;
    }

    if (value->IsString())
    {
        Local<String> text = value.As<String>();
        const int length = text->Length();
        // A one-byte string holds one byte per code unit, so it does not have to
        // be encoded as UTF-8 only for Python to decode it again.  Short ones are
        // copied to the stack as they are; anything longer keeps the general path
        // rather than trading an allocation for a second buffer.
        if (text->IsOneByte() && length <= kInlineStringBytes)
        {
            uint8_t buffer[kInlineStringBytes];
            text->WriteOneByte(isolate, 0, static_cast<uint32_t>(length), buffer);
            // Latin-1 covers ASCII too. CPython selects the compact ASCII
            // representation itself; a separate scan here repeated that work.
            return PyUnicode_DecodeLatin1(reinterpret_cast<const char*>(buffer), length, "replace");
        }
        if (text->IsOneByte())
        {
            String::Utf8Value utf8(isolate, text);
            return PyUnicode_DecodeUTF8(*utf8, utf8.length(), "replace");
        }
        // V8 strings can carry unpaired UTF-16 surrogates. Keep those code
        // units when materializing a value such as a page's response text.
        uint16_t inline_units[kInlineStringBytes];
        std::unique_ptr<uint16_t[]> dynamic_units;
        uint16_t* units = inline_units;
        if (length > kInlineStringBytes)
        {
            dynamic_units.reset(new (std::nothrow) uint16_t[length]);
            if (!dynamic_units)
                return PyErr_NoMemory();
            units = dynamic_units.get();
        }
        text->Write(isolate, 0, static_cast<uint32_t>(length), units);
        const uint16_t endian_marker = 1;
        int byte_order = *reinterpret_cast<const uint8_t*>(&endian_marker) ? -1 : 1;
        return PyUnicode_DecodeUTF16(reinterpret_cast<const char*>(units),
                                    static_cast<Py_ssize_t>(length) * 2,
                                    "surrogatepass", &byte_order);
    }

    if (value->IsArray())
    {
        Local<Array> array = value.As<Array>();
        PyObject* existing = conversion->find(array);
        if (existing != NULL || PyErr_Occurred())
            return existing;

        const uint32_t length = array->Length();
        PyObject *list = PyList_New(length);
        if (!list)
            return NULL;
        if (!conversion->remember(array, list))
        {
            Py_DECREF((PyObject*)list);
            return NULL;
        }

        for (uint32_t i = 0; i < length; i++)
        {
            Local<Value> js_value;
            if (!without_gil([&] { return array->Get(context, i).ToLocal(&js_value); }))
            {
                Py_DECREF((PyObject*)list);
                return NULL;
            }

            PyObject *obj = py_from_js_impl(js_value, context, conversion);
            if (obj == NULL)
            {
                Py_DECREF((PyObject*)list);
                return NULL;
            }
            PyList_SetItem(list, i, obj);
        }
        return list;
    }

    if (value->IsObject())
    {
        Local<Object> obj_value = value.As<Object>();
        if (context.IsEmpty())
        {
            context = obj_value->GetCreationContextChecked();
        }
        // Native-backed objects already carry their Python identity. Inspect
        // it before probing JS properties: Window security interceptors may
        // reject even a Symbol.toStringTag query on a cross-origin facade.
        if (obj_value->InternalFieldCount() == OBJECT_INTERNAL_FIELDS)
        {
            Local<Data> magic = obj_value->GetInternalField(PYTHON_MAGIC_FIELD);
            if (isolate_state != nullptr &&
                magic == isolate_state->v8_string(V8StringId::PythonObjectMagic))
            {
                PyObject* object = IsolateState::python_object(obj_value);
                if (!object) return nullptr;
                Py_INCREF((PyObject*)object);
                return object;
            }
        }
        bool has_to_string_tag;
        Maybe<bool> maybe_has_to_string_tag = without_gil([&] {
            return obj_value->HasOwnProperty(
                context, Symbol::GetToStringTag(v8_compat::context_isolate(context)));
        });
        if (!maybe_has_to_string_tag.To(&has_to_string_tag))
        {
            PyErr_SetString(PyExc_RuntimeError, "failed to inspect JavaScript object");
            return NULL;
        }
        if (obj_value->GetPrototype()->StrictEquals(context->GetEmbedderData(OBJECT_PROTOTYPE_SLOT)) && !has_to_string_tag)
        {
            PyObject* existing = conversion->find(obj_value);
            if (existing != NULL || PyErr_Occurred())
                return existing;

            PyObject *dict = PyDict_New();
            if (!dict)
                return NULL;
            if (!conversion->remember(obj_value, dict))
            {
                Py_DECREF((PyObject*)dict);
                return NULL;
            }

            Local<Array> js_keys;
            if (!without_gil([&] { return obj_value->GetPropertyNames(context).ToLocal(&js_keys); }))
            {
                Py_XDECREF((PyObject*)dict);
                PyErr_SetString(PyExc_RuntimeError, "failed to get JavaScript object properties");
                return NULL;
            }
            for (uint32_t i = 0; i < js_keys->Length(); i++)
            {
                Local<Value> js_key;
                // GetPropertyNames creates a private dense array of data keys.
                // Reading it cannot reach user getters, unlike the source
                // object's values below, which still run without the GIL.
                if (!js_keys->Get(context, i).ToLocal(&js_key))
                {
                    Py_XDECREF((PyObject*)dict);
                    PyErr_SetString(PyExc_RuntimeError, "failed to get JavaScript object property key");
                    return NULL;
                }
                PyObject *key = py_from_js_impl(js_key, context, conversion);
                if (key == NULL)
                {
                    Py_XDECREF((PyObject*)dict);
                    return NULL;
                }
                Local<Value> js_value;
                if (!without_gil([&] { return obj_value->Get(context, js_key).ToLocal(&js_value); }))
                {
                    Py_XDECREF((PyObject*)key);
                    Py_XDECREF((PyObject*)dict);
                    PyErr_SetString(PyExc_RuntimeError, "failed to get JavaScript object property value");
                    return NULL;
                }
                PyObject *dict_value = py_from_js_impl(js_value, context, conversion);
                if (dict_value == NULL)
                {
                    Py_XDECREF((PyObject*)dict);
                    Py_XDECREF((PyObject*)key);
                    return NULL;
                }
                if (PyDict_SetItem(dict, key, dict_value) < 0)
                {
                    Py_XDECREF((PyObject*)key);
                    Py_XDECREF((PyObject*)dict_value);
                    Py_XDECREF((PyObject*)dict);
                    return NULL;
                }
                Py_XDECREF((PyObject*)key);
                Py_XDECREF((PyObject*)dict_value);
            }
            return dict;
        }

        return (PyObject *)js_object_new(obj_value, context);
    }

    if (value->IsUint32() || value->IsInt32())
    {
        return PyLong_FromLongLong((PY_LONG_LONG)value.As<Integer>()->Value());
    }
    if (value->IsNumber())
    {
        return PyFloat_FromDouble(value.As<Number>()->Value());
    }

    if (value->IsBigInt()) {
        return (PyObject*)js_value_new(value, context);
    }
    // Nothing above claims it.  For a JavaScript value that means one of the
    // exotic kinds this boundary does not translate -- an external, a private, a
    // wasm value -- and saying so is better than a value that quietly arrives in
    // Python as None.
    PyErr_Format(PyExc_TypeError, "cannot convert this JavaScript value to Python");
    return nullptr;
}

PyObject *
py_materialize(Local<Value> value, Local<Context> context)
{
    JsToPyConversionContext conversion;
    return py_from_js_impl(value, context, &conversion);
}

PyObject *
py_from_js(Local<Value> value, Local<Context> context)
{
    if (value->IsSymbol())
        return (PyObject*)js_value_new(value, context);
    if (value->IsObject()) {
        Local<Object> object = value.As<Object>();
        auto* state = runtime_manager.find_isolate(v8_compat::context_isolate(context));
        if (object->InternalFieldCount() == OBJECT_INTERNAL_FIELDS && state != nullptr &&
            object->GetInternalField(PYTHON_MAGIC_FIELD) == state->v8_string(V8StringId::PythonObjectMagic)) {
            auto* result = IsolateState::python_object(object);
            if (!result) return nullptr;
            Py_INCREF((PyObject*)result);
            return result;
        }
        // Do not enumerate, probe prototypes, read accessors, or iterate here.
        return (PyObject*)js_object_new(object, context);
    }
    return py_from_js_impl(value, context, nullptr);
}
