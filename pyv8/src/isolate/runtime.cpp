#include "bridge_base.h"
#include "entry.h"
#include "runtime.h"

#include "isolate_state.h"
#include "isolate_scope.h"
#include "isolate.h"
#include "serialization.h"

#include <new>

RuntimeManager runtime_manager;

IsolateState* select_isolate(IsolateSelection policy, PyObject* owner) {
    switch (policy) {
    case IsolateSelection::ActiveOrDefault:
        if (auto* active = current_isolate_state()) return active;
        return runtime_manager.default_isolate();
    case IsolateSelection::Explicit:
        return isolate_state_from_python(owner, false);
    case IsolateSelection::DefaultOnly:
        return runtime_manager.default_isolate();
    }
    return nullptr;
}


RuntimeManager::RuntimeManager() = default;

RuntimeManager::~RuntimeManager()
{
    // Explicit shutdown owns teardown; preserve the previous DLL-unload behavior.
    default_isolate_.release();
    custom_isolates_.clear();
    // The owned configuration is deliberately not released here.  This object lives
    // as long as the process, and the destructor runs while the interpreter is being
    // finalized (or after the DLL is unloaded), where touching Python refcounts is
    // not safe.  configure() releases what it replaces while the GIL is held.
}

bool
RuntimeManager::configure(PyObject* icu, PyObject* params)
{
    if (is_initialized())
    {
        PyErr_SetString(
            PyExc_RuntimeError,
            "V8 has already started; pyv8.configure() must be called before the first use");
        return false;
    }
    if (platform_.is_disposed())
    {
        PyErr_SetString(PyExc_RuntimeError, "V8 has been shut down");
        return false;
    }
    // Validated here rather than at first use so a wrong argument is reported where
    // it was passed; the same check runs again in PlatformState::initialize.
    if (!PlatformState::validate_config(icu, params))
        return false;

    PyObject* previous_icu = configured_icu_path_;
    Py_XINCREF(icu);
    configured_icu_path_ = icu;
    Py_XDECREF(previous_icu);

    PyObject* previous_params = configured_params_;
    Py_XINCREF(params);
    configured_params_ = params;
    Py_XDECREF(previous_params);
    return true;
}

void
RuntimeManager::set_default_icu_path(PyObject* path)
{
    PyObject* previous = default_icu_path_;
    Py_XINCREF(path);
    default_icu_path_ = path;
    Py_XDECREF(previous);
}

PyObject*
RuntimeManager::pending_icu_path() const
{
    return configured_icu_path_ != nullptr ? configured_icu_path_ : default_icu_path_;
}

bool
RuntimeManager::initialize(PyObject* icu, PyObject* params)
{
    if (is_initialized())
        return false;
    if (!platform_.initialize(icu, params))
        return false;

    std::unique_ptr<IsolateState> state(new (std::nothrow) IsolateState());
    if (!state)
    {
        PyErr_NoMemory();
        platform_.shutdown();
        return false;
    }
    if (!state->initialize())
    {
        platform_.shutdown();
        return false;
    }
    default_isolate_ = std::move(state);
    return true;
}

bool
RuntimeManager::is_initialized() const
{
    return default_isolate_ != nullptr && default_isolate_->is_active();
}

bool
RuntimeManager::ensure_initialized()
{
    if (is_initialized())
        return true;
    if (platform_.is_disposed())
    {
        PyErr_SetString(PyExc_RuntimeError, "V8 has been shut down");
        return false;
    }
    // Startup runs with the GIL held -- every entry point here is reached through a
    // Python call -- so this flag only has to catch a start that is already under
    // way, not to serialize two threads.
    if (initializing_)
    {
        PyErr_SetString(PyExc_RuntimeError, "V8 initialization is already in progress");
        return false;
    }
    initializing_ = true;
    const bool started = initialize(pending_icu_path(), configured_params_);
    initializing_ = false;
    return started;
}

IsolateState*
RuntimeManager::create_isolate(bool thread_bound)
{
    if (!ensure_initialized())
        return nullptr;

    IsolateState* state = new (std::nothrow) IsolateState(thread_bound);
    if (state == nullptr)
    {
        PyErr_NoMemory();
        return nullptr;
    }
    if (!state->initialize())
    {
        delete state;
        return nullptr;
    }
    try
    {
        custom_isolates_.insert(state);
    }
    catch (const std::bad_alloc&)
    {
        state->shutdown(false);
        delete state;
        PyErr_NoMemory();
        return nullptr;
    }
    return state;
}

bool
RuntimeManager::destroy_isolate(IsolateState* state, bool report_error)
{
    if (state == nullptr || state == default_isolate_.get())
    {
        if (report_error)
            PyErr_SetString(PyExc_RuntimeError, "the default isolate is owned by shutdown_v8()");
        return false;
    }
    auto item = custom_isolates_.find(state);
    if (item == custom_isolates_.end())
        return false;
    if (!state->shutdown(report_error))
        return false;
    custom_isolates_.erase(item);
    delete state;
    return true;
}

IsolateState*
RuntimeManager::find_isolate(v8::Isolate* isolate) const
{
    return isolate ? static_cast<IsolateState*>(isolate->GetData(0)) : nullptr;
}

bool
RuntimeManager::shutdown(bool report_error)
{
    // Never starts V8: a runtime that was never used has nothing to shut down, and
    // shutdown_v8() must not be the call that brings it up.
    if (!is_initialized())
        return false;
    if (!custom_isolates_.empty())
    {
        if (report_error)
        {
            PyErr_Format(PyExc_RuntimeError,
                         "cannot shut down V8 while %zu custom isolate(s) are active",
                         custom_isolates_.size());
        }
        return false;
    }
    const auto compiled_modules = live_compiled_wasm_modules();
    if (compiled_modules != 0) {
        if (report_error)
            PyErr_Format(PyExc_RuntimeError,
                         "cannot shut down V8 while %zu serialized WebAssembly module(s) are alive",
                         compiled_modules);
        return false;
    }
    if (!default_isolate_->shutdown(report_error))
        return false;
    default_isolate_.reset();
    platform_.shutdown();
    return true;
}
