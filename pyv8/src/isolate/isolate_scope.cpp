#include "bridge_base.h"
#include "entry.h"
#include "isolate_scope.h"

#include "gil_scope.h"
#include "isolate_state.h"
#include "callback_boundary.h"
#include "context.h"

#include <Python.h>

static thread_local IsolateState* active_isolate_state = nullptr;

IsolateCleanupScope::IsolateCleanupScope(IsolateState* state, PyObject* object,
                                       int (*clear)(PyObject*)) {
    if (!state) return;
    lock_.reset(new IsolateLockScope(state, true));
    if (!lock_->entered()) state->defer_cleanup(object, clear);
}

IsolateState*
current_isolate_state()
{
    return active_isolate_state;
}

bool IsolateLockScope::has_thread_entry(IsolateState* state)
{
    auto* active = current_isolate_state();
    if (active == nullptr) return false;
    if (active == state) return true;
    // A successful recursive acquisition can only see this thread's outer
    // entry or an idle isolate. Never wait for another thread while nested.
    std::unique_lock<std::recursive_mutex> lock(state->entry_mutex, std::try_to_lock);
    if (!lock.owns_lock()) {
        PyErr_SetString(PyExc_RuntimeError,
            "cannot wait for another V8 isolate during nested execution");
        return false;
    }
    return state->entry_depth != 0;
}

IsolateLockScope::IsolateLockScope(IsolateState* state, bool try_only)
    : state_(state),
      previous_(current_isolate_state()),
      isolate_(nullptr),
      entered_(false),
      nested_(false),
      python_owner_(nullptr)
{
    if (state == current_isolate_state()) {
        isolate_ = state->isolate();
        nested_ = entered_ = true;
        ++state_->entry_depth;
        return;
    }
    if (!state->on_owner_thread()) {
        if (!try_only) PyErr_SetString(PyExc_RuntimeError,
            "thread-bound V8 isolate requires its owner thread");
        return;
    }
    // Never block on a second isolate while holding another isolate's lock.
    // Uncontended nesting (including returning to an already-held isolate) works.
    const bool cross_isolate = previous_ != nullptr;
    const bool nonblocking = try_only || cross_isolate;
    if (nonblocking && !state_->entry_mutex.try_lock()) {
        if (!try_only)
            PyErr_SetString(PyExc_RuntimeError,
                "cannot wait for another V8 isolate during nested execution");
        return;
    }
    python_owner_ = state_->python_owner;
    Py_XINCREF((PyObject*)python_owner_);
    ++state_->entry_users;
    bool locked = nonblocking;
    bool closed = false;
    try
    {
        // Waiting (mutex and locker) must not hold the GIL; the scope restores
        // it before this function returns.
        GILReleaseScope waiting;
        if (!nonblocking) state_->entry_mutex.lock();
        locked = true;
        isolate_ = state_->isolate();
        if (isolate_ == nullptr)
        {
            state_->entry_mutex.unlock();
            locked = false;
            closed = true;
        }
        else
        {
            // A bound isolate never migrates and has never used V8's Locker.
            // Its thread state (including compiler interrupts) stays intact
            // between owner turns. Migratable isolates still need the Locker.
            if (!state_->thread_bound()) locker_.emplace(isolate_);
        }
    }
    catch (...)
    {
        if (locked) state_->entry_mutex.unlock();
        --state_->entry_users;
        Py_XDECREF((PyObject*)python_owner_);
        python_owner_ = nullptr;
        throw;
    }
    if (closed)
    {
        --state_->entry_users;
        Py_XDECREF((PyObject*)python_owner_);
        python_owner_ = nullptr;
        PyErr_SetString(PyExc_RuntimeError, "V8 isolate has been closed");
        return;
    }
    if (state_->entry_depth == 0)
    {
        // A crossing that begins now starts clean: a failure the host ended the last
        // one for was raised at its own boundary and is not this one's to inherit.
        state_->clear_aborted_failure();
    }
    isolate_->Enter();
    entered_ = true;
    // It lives on the thread's V8 state, so every entry has to set it.
    context_install_access_check(isolate_);
    ++state_->entry_depth;
    active_isolate_state = state_;
}

IsolateLockScope::~IsolateLockScope()
{
    if (!entered_) return;
    if (nested_) {
        --state_->entry_depth;
        return;
    }
    if (state_->entry_depth == 1) {
        v8::HandleScope handle_scope(isolate_);
        state_->drain_deferred();
    }
    active_isolate_state = previous_;
    if (entered_)
    {
        isolate_->Exit();
        entered_ = false;
    }
    locker_.reset();
    std::vector<PyObject*> waiters;
    if (--state_->entry_depth == 0) waiters.swap(state_->inspector_waiters);
    state_->entry_mutex.unlock();
    if (!waiters.empty()) {
        PyObject *error_type, *error_value, *error_traceback;
        PyErr_Fetch(&error_type, &error_value, &error_traceback);
        for (auto callback : waiters) {
            PyObject* result = PyObject_CallNoArgs(callback);
            if (!result) PyErr_WriteUnraisable(callback);
            else Py_DECREF((PyObject*)result);
            Py_DECREF((PyObject*)callback);
        }
        PyErr_Restore(error_type, error_value, error_traceback);
    }
    --state_->entry_users;
    Py_XDECREF((PyObject*)python_owner_);
}
