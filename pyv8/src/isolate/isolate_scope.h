#ifndef PYV8_ISOLATE_SCOPE_H
#define PYV8_ISOLATE_SCOPE_H

#include "bridge_base.h"
#include <memory>
#include <optional>

#include <v8.h>
#include <Python.h>

class IsolateState;

IsolateState* current_isolate_state();

class IsolateLockScope
{
public:
    // Observe existing entries without starting a V8 thread/Locker. A contended
    // foreign isolate retains the nested-execution error contract.
    static bool has_thread_entry(IsolateState* state);
    explicit IsolateLockScope(IsolateState* state, bool try_only = false);
    ~IsolateLockScope();

    IsolateLockScope(const IsolateLockScope&) = delete;
    IsolateLockScope& operator=(const IsolateLockScope&) = delete;

    v8::Isolate* isolate() const { return isolate_; }
    bool entered() const { return entered_; }

private:
    IsolateState* state_;
    IsolateState* previous_;
    v8::Isolate* isolate_;
    std::optional<v8::Locker> locker_;
    bool entered_;
    bool nested_;
    bool datetime_pushed_;
    PyObject* python_owner_;
};

// tp_clear/tp_dealloc may run on any Python thread, including the Inspector
// transport. Keep the object alive until the active entry can clear it safely.
class IsolateCleanupScope {
public:
    IsolateCleanupScope(IsolateState* state, PyObject* object, int (*clear)(PyObject*));
    bool ready() const { return !lock_ || lock_->entered(); }
private:
    std::unique_ptr<IsolateLockScope> lock_;
};

#endif
