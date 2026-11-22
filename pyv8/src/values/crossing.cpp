#include "bridge_base.h"
#include <Python.h>
#include "entry.h"
#include <v8.h>

#include "convert.h"
#include "crossing.h"
#include "error_family.h"
#include "handle_resource.h"
#include "js_exception.h"

using namespace v8;

void
py_throw_js(Local<Value> js_exc, Local<Message> js_message)
{
    IsolateState* isolate_state = current_isolate_state();
    if (isolate_state == nullptr) {
        PyErr_SetString(PyExc_RuntimeError, "no active V8 isolate");
        return;
    }
    bool is_python_object = false;
    if (js_exc->IsObject() &&
        js_exc.As<Object>()->InternalFieldCount() == OBJECT_INTERNAL_FIELDS) {
        Local<Data> magic = js_exc.As<Object>()->GetInternalField(PYTHON_MAGIC_FIELD);
        is_python_object =
            magic == isolate_state->v8_string(V8StringId::PythonObjectMagic);
    }
    if (is_python_object) {
        Local<Object> exc_object = js_exc.As<Object>();
        IsolateState::restore_python_exception(exc_object);
    }
    else {
        // A failed materialization may set a fallback Python error while V8
        // also caught the original getter/proxy exception. Its Python wrapper
        // must be constructed with a clean error indicator, or CPython turns
        // the successful constructor result into a SystemError.
        PyErr_Clear();
        PyObject* exception = js_exception_new(js_exc, js_message);
        if (exception == NULL) {
            return;
        }
        PyErr_SetObject((PyObject*)js_exception_type, exception);
        Py_DECREF((PyObject*)exception);
    }
}

void
js_throw_py()
{
    IsolateState* isolate_state = current_isolate_state();
    if (isolate_state == nullptr || !isolate_state->is_active()) {
        PyErr_SetString(PyExc_RuntimeError, "no active V8 isolate");
        return;
    }
    Isolate* isolate = isolate_state->isolate();
    Local<Context> context = isolate->GetCurrentContext();
    PyObject* exc_type, * exc_value, * exc_traceback;
    PyErr_Fetch(&exc_type, &exc_value, &exc_traceback);
    PyErr_NormalizeException(&exc_type, &exc_value, &exc_traceback);

    if (exc_value && PyObject_TypeCheck(exc_value, js_exception_type)) {
        JsExceptionState* state = js_exception_get_state(exc_value);
        if (state && state->isolate_state == isolate_state) {
            isolate->ThrowException(state->exception.Get(isolate));
            Py_XDECREF((PyObject*)exc_type);
            Py_XDECREF((PyObject*)exc_value);
            Py_XDECREF((PyObject*)exc_traceback);
            return;
        }
        PyErr_Clear();
    }

    const char* family = error_family::family_name(Py_TYPE(exc_value));
    if (family == nullptr) {
        // The host's own failure: the realm is not shown it, so the crossing ends
        // where it started and the exception waits for the boundary that ran the
        // script.  The thrown error is there to end the crossing as a failed read or
        // call -- JavaScript cannot reach it, which is what the termination is for --
        // and the boundary raises what is held instead.
        if (exc_value != nullptr && !isolate_state->has_aborted_failure()) {
            isolate_state->hold_aborted_failure(exc_type, exc_value, exc_traceback);
        }
        else {
            // Nothing new to carry: what is already held is the failure this crossing
            // ends for, and a call with nothing of its own must not replace it.
            Py_XDECREF((PyObject*)exc_type);
            Py_XDECREF((PyObject*)exc_value);
            Py_XDECREF((PyObject*)exc_traceback);
        }
        isolate->ThrowException(Exception::Error(V8String(isolate, "Python failure kept from JavaScript")));
        isolate->TerminateExecution();
        // Nothing is left set here: the failure is held on the isolate, and the boundary
        // that began this execution raises it (``resume_aborted_crossing``).  Leaving one
        // pending would find the next Python call that runs before that boundary with an
        // exception it never asked for.
        return;
    }

    PyObject* exc_value_str = pyexception_message(exc_value);
    const char* exc_message = "Python exception";
    if (exc_value_str != NULL) {
        const char* message = PyUnicode_AsUTF8_abi3(exc_value_str);
        if (message != NULL) {
            exc_message = message;
        }
        else {
            PyErr_Clear();
        }
    }
    else {
        PyErr_Clear();
    }

    Local<Object> exception = error_family::build(isolate, context, family, exc_value, exc_message);
    if (exception.IsEmpty())
        exception = Exception::Error(V8String(isolate, exc_message)).As<Object>();

    Py_XDECREF((PyObject*)exc_value_str);
    Py_XDECREF((PyObject*)exc_type);
    Py_XDECREF((PyObject*)exc_value);
    Py_XDECREF((PyObject*)exc_traceback);

    isolate->ThrowException(exception);
}

bool
resume_aborted_crossing()
{
    IsolateState* isolate_state = current_isolate_state();
    if (isolate_state == nullptr)
        return false;
    return isolate_state->resume_aborted_failure();
}
