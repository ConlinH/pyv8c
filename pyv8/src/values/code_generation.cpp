// Dynamic compilation crosses both the V8 value and Python error boundaries.
#include "bridge_base.h"
#include "context.h"
#include "convert.h"
#include "crossing.h"
#include "execution.h"

PyObject* context_set_code_generation_handler(context_c* self, PyObject* handler) {
    if (handler != Py_None && !PyCallable_Check(handler)) {
        PyErr_SetString(PyExc_TypeError, "code generation handler must be callable or None");
        return nullptr;
    }
    ExecutionScope execution(self);
    if (!execution) return nullptr;
    PyObject* replacement = handler == Py_None ? nullptr : handler;
    Py_XINCREF(replacement);
    PyObject* previous = self->code_generation_handler;
    self->code_generation_handler = replacement;
    execution.context()->AllowCodeGenerationFromStrings(replacement == nullptr);
    Py_XDECREF(previous);
    Py_RETURN_NONE;
}

v8::ModifyCodeGenerationFromStringsResult context_modify_code_generation(
        v8::Local<v8::Context> context, v8::Local<v8::Value> source, bool code_like) {
    GILAcquireScope acquire;
    v8::ModifyCodeGenerationFromStringsResult answer;
    try {
        auto* owner = context_from_v8(context);
        if (!context_is_active(owner)) return answer;
        if (owner->code_generation_handler == nullptr) {
            if (source->IsObject() && source.As<v8::Object>()->IsCodeLike(v8::Isolate::GetCurrent())) {
                v8::Local<v8::String> text;
                if (!source->ToString(context).ToLocal(&text)) return answer;
                answer.modified_source = text;
            }
            answer.codegen_allowed = true;
            return answer;
        }
        PyObject* handler = owner->code_generation_handler;
        Py_INCREF(handler);
        PyOwnedRef keep_handler(handler);
        PyOwnedRef input(py_from_js(source, context));
        if (!input.get()) { js_throw_py(); return answer; }
        PyOwnedRef result(PyObject_CallFunctionObjArgs(handler, input.get(),
                                                       code_like ? Py_True : Py_False, nullptr));
        if (!result.get()) { js_throw_py(); return answer; }
        if (PyUnicode_Check(result.get())) {
            auto modified = js_from_py(result.get(), context);
            if (modified.IsEmpty()) { js_throw_py(); return answer; }
            answer.modified_source = modified.As<v8::String>();
            answer.codegen_allowed = true;
        } else {
            answer.codegen_allowed = result.get() == Py_True;
        }
        return answer;
    } catch (...) {
        translate_native_exception();
        js_throw_py();
        return answer;
    }
}
