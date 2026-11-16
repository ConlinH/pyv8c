#include "bridge_base.h"
#include "entry.h"
#include "isolate_state.h"

#include "debugger.h"
#include "error_family.h"
#include "error_hooks.h"
#include "isolate_scope.h"
#include "callback_boundary.h"
#include "context.h"
#include "pyclass.h"
#include "v8_compat.h"
#include "runtime.h"
#include "wasm_streaming.h"

#include <cassert>
#include <new>
#include <limits>

#if defined(_WIN32)
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#endif

using namespace v8;

IsolateState::IsolateState()
    : external_memory_bytes_(0),
      isolate_(nullptr),
      live_v8_handles_(0),
      error_hooks_installed_(false),
      aborted_type_(nullptr),
      aborted_value_(nullptr),
      aborted_traceback_(nullptr),
      disposed_(false)
{
}

IsolateState::~IsolateState()
{
    Py_CLEAR(wasm_response_handler_);
    clear_aborted_failure();
    // Avoid V8 teardown during arbitrary extension-module unloading.
    if (isolate_ != nullptr)
    {
        v8_strings_.release();
        allocator_.release();
    }
}

void
IsolateState::hold_aborted_failure(PyObject* exc_type, PyObject* exc_value, PyObject* exc_traceback)
{
    // The references belong to the slot from here on, whether or not V8 answers.
    clear_aborted_failure();
    aborted_type_ = exc_type;
    aborted_value_ = exc_value;
    aborted_traceback_ = exc_traceback;
    if (isolate_ != nullptr)
        isolate_->TerminateExecution();
}

bool
IsolateState::resume_aborted_failure()
{
    if (aborted_value_ == nullptr)
        return false;
    // The failure stays held until the next entry starts: a boundary inside the
    // unwinding may swallow it, and the crossing that began the execution still has
    // to hear it.
    Py_XINCREF(aborted_type_);
    Py_INCREF(aborted_value_);
    Py_XINCREF(aborted_traceback_);
    // The abort was the host's, so the isolate is free to run the next crossing.
    if (isolate_ != nullptr)
        isolate_->CancelTerminateExecution();
    PyErr_Restore(aborted_type_, aborted_value_, aborted_traceback_);
    return true;
}

void
IsolateState::clear_aborted_failure()
{
    Py_CLEAR(aborted_type_);
    Py_CLEAR(aborted_value_);
    Py_CLEAR(aborted_traceback_);
}

std::size_t
IsolateState::near_heap_limit_callback(void* data, std::size_t current_limit,
                                       std::size_t initial_limit)
{
    IsolateState* state = static_cast<IsolateState*>(data);
    if (state == nullptr || initial_limit == 0)
        return current_limit;

    // The script is stopped at the first sight of the limit: V8 aborts inside its
    // own collection a few retries later, and by then nothing can be said.  The
    // watchdog thread makes the call, where V8 can hear it.
    if (state->watchdog_ != nullptr)
        state->watchdog_->check_near_heap_limit(initial_limit);

    // V8 checks the limit immediately after this callback and can abort without
    // another retry. Small incremental grants may remain below a large live
    // backing store even though most of the allowed reserve is still unused.
    // The stop is already pending, so grant the existing full unwind allowance
    // now; do not depend on whether the watchdog thread has run yet.
    if (initial_limit > std::numeric_limits<std::size_t>::max() / kHeapUnwindLimitFactor)
        return current_limit;
    const std::size_t ceiling = initial_limit * kHeapUnwindLimitFactor;
    // Only the actual ceiling matters, not the lifetime sum of prior grants:
    // V8 may shrink its limit after collection. Never grow beyond this bound.
    return current_limit < ceiling ? ceiling : current_limit;
}

void
IsolateState::account_external_memory(int64_t delta)
{
    if (isolate_ == nullptr || delta == 0)
        return;
    external_memory_bytes_ += delta;
    if (external_memory_bytes_ < 0)
        external_memory_bytes_ = 0;
    external_memory_.Update(isolate_, delta);
}

void IsolateState::collected_heap_callback(Isolate*, GCType, GCCallbackFlags, void* data)
{
    auto* state = static_cast<IsolateState*>(data);
    if (state != nullptr && state->watchdog_ != nullptr)
        state->watchdog_->check_collected_heap();
}

bool
IsolateState::initialize()
{
    if (is_active())
        return true;
    if (disposed_)
    {
        PyErr_SetString(PyExc_RuntimeError, "V8 isolate has been closed");
        return false;
    }

    allocator_.reset(ArrayBuffer::Allocator::NewDefaultAllocator());
    if (!allocator_)
    {
        PyErr_SetString(PyExc_RuntimeError, "failed to create V8 ArrayBuffer allocator");
        return false;
    }

    Isolate::CreateParams create_params;
    create_params.array_buffer_allocator = allocator_.get();
#if defined(_WIN32)
    // Chromium configures V8 from the host's physical and virtual memory.
    // Leave V8's defaults in place if Windows cannot provide the information;
    // explicit V8 heap flags still take precedence over these constraints.
    MEMORYSTATUSEX memory_status = {};
    memory_status.dwLength = sizeof(memory_status);
    if (GlobalMemoryStatusEx(&memory_status))
        create_params.constraints.ConfigureDefaults(memory_status.ullTotalPhys,
                                                    memory_status.ullTotalVirtual);
#endif
    isolate_ = Isolate::New(create_params);
    if (isolate_ == nullptr)
    {
        allocator_.reset();
        PyErr_SetString(PyExc_RuntimeError, "failed to create V8 isolate");
        return false;
    }

    isolate_->SetData(0, this);
    isolate_->SetWasmStreamingCallback(CallbackBoundary<wasm_streaming_callback>::call);
    isolate_->AddNearHeapLimitCallback(&IsolateState::near_heap_limit_callback, this);
    // What ``Error.isError`` hears about this binding's API objects; Blink answers the same
    // for its DOMExceptions (v8_initializer.cc, IsDOMExceptionWrapper).
    isolate_->SetIsJSApiWrapperNativeErrorCallback(
        CallbackBoundary<error_family::is_native_error_wrapper>::call);
    watchdog_ = std::make_unique<Watchdog>(isolate_);
    isolate_->AddGCEpilogueCallback(&IsolateState::collected_heap_callback, this);
    isolate_->SetCaptureStackTraceForUncaughtExceptions(
        true, 100,
        static_cast<StackTrace::StackTraceOptions>(
            StackTrace::kOverview | StackTrace::kScriptId));

    try
    {
        v8_strings_ = std::make_unique<V8StringCache>();
    }
    catch (const std::bad_alloc&)
    {
        runtime_manager.notify_isolate_shutdown(isolate_);
        isolate_->Dispose();
        isolate_ = nullptr;
        allocator_.reset();
        PyErr_NoMemory();
        return false;
    }

    bool strings_initialized;
    {
        IsolateLockScope scope(this);
        HandleScope handle_scope(isolate_);
        strings_initialized = v8_strings_->initialize(isolate_);
    }
    if (!strings_initialized)
    {
        v8_strings_.reset();
        runtime_manager.notify_isolate_shutdown(isolate_);
        isolate_->Dispose();
        isolate_ = nullptr;
        allocator_.reset();
        PyErr_SetString(PyExc_RuntimeError, "failed to initialize V8 string cache");
        return false;
    }
    return true;
}

bool
IsolateState::require_active() const
{
    if (isolate_ != nullptr)
        return true;
    PyErr_SetString(PyExc_RuntimeError,
                    disposed_ ? "V8 isolate has been closed"
                              : "V8 isolate has not been initialized");
    return false;
}

void
IsolateState::retain_v8_handle()
{
    ++live_v8_handles_;
}

void
IsolateState::release_v8_handle()
{
    if (live_v8_handles_ > 0)
        --live_v8_handles_;
}

bool
IsolateState::shutdown(bool report_error)
{
    if (!is_active())
        return false;

    if (entry_users != 0 || entry_depth != 0) {
        if (report_error) PyErr_SetString(PyExc_RuntimeError, "cannot close V8 isolate during active execution");
        return false;
    }
    // A failure no boundary collected would otherwise outlive the isolate holding it.
    clear_aborted_failure();

    // Shared interface templates hold persistent handles for the isolate's lifetime;
    // release them before the teardown counts what is left.
    if (!shared_interfaces_.empty())
    {
        IsolateLockScope scope(this);
        HandleScope handle_scope(isolate_);
        for (auto& entry : shared_interfaces_)
        {
            py_class_release_handles(entry.second);
            Py_DECREF(entry.first);
            Py_DECREF(entry.second);
        }
        shared_interfaces_.clear();
        drain_deferred();
    }

    if (live_v8_handles_ != 0)
    {
        IsolateLockScope scope(this);
        HandleScope handle_scope(isolate_);
        isolate_->LowMemoryNotification();
        drain_deferred();
    }
    if (live_v8_handles_ != 0)
    {
        if (report_error)
        {
            PyErr_Format(PyExc_RuntimeError,
                         "cannot close V8 isolate while %zu persistent handle(s) are active",
                         live_v8_handles_);
        }
        return false;
    }

    {
        IsolateLockScope scope(this);
        HandleScope handle_scope(isolate_);
        // The reporters go first: they hold references into Python and could
        // otherwise be called while the isolate is already being torn down.
        error_hooks_uninstall(this);
        // Inspectors go with the isolate that owns them, and this is the last
        // point at which that is still true.
        debugger_isolate_closed(isolate_);
        clear_python_owners();
        shared_wrappers_.clear();
        drain_deferred();
        for (auto* data : callback_data_) {
            data->external.Reset();
            Py_XDECREF(data->owned_target);
            delete data;
        }
        callback_data_.clear();
        v8_strings_->reset();
    }

    // The isolate is about to go away, and a watchdog that reached it afterwards
    // would be a call into freed memory.  A shutdown that failed above left it
    // running, which is what keeps a still-open isolate on its deadline.
    if (watchdog_ != nullptr)
        watchdog_->shutdown();

    Py_CLEAR(wasm_response_handler_);

    v8_strings_.reset();
    // The accounter's invariant: what it reported goes back to zero before disposal.
    if (external_memory_bytes_ != 0)
        external_memory_.Update(isolate_, -external_memory_bytes_);
    external_memory_bytes_ = 0;
    isolate_->RemoveNearHeapLimitCallback(&IsolateState::near_heap_limit_callback, 0);
    isolate_->RemoveGCEpilogueCallback(&IsolateState::collected_heap_callback, this);
    runtime_manager.notify_isolate_shutdown(isolate_);
    isolate_->Dispose();
    isolate_ = nullptr;
    allocator_.reset();
    disposed_ = true;
    return true;
}

bool IsolateState::set_wasm_response_handler(PyObject* handler)
{
    if (handler != Py_None && !PyCallable_Check(handler)) {
        PyErr_SetString(PyExc_TypeError, "WASM response handler must be callable or None");
        return false;
    }
    if (handler == Py_None) handler = nullptr;
    Py_XINCREF(handler);
    Py_XDECREF(wasm_response_handler_);
    wasm_response_handler_ = handler;
    return true;
}
