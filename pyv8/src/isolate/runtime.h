#ifndef PYV8_RUNTIME_H
#define PYV8_RUNTIME_H

#include "bridge_base.h"
#include <Python.h>

#ifdef _WIN32
#undef COMPILER
#ifndef NO_RETURN
#define NO_RETURN __declspec(noreturn)
#endif
#endif

#include <v8.h>

#include "platform_state.h"

#include <memory>
#include <unordered_set>

class IsolateState;
enum class IsolateSelection { DefaultOnly, ActiveOrDefault, Explicit };
IsolateState* select_isolate(IsolateSelection policy, PyObject* explicit_owner = nullptr);

// The process-global V8 runtime.
//
// Nothing here runs until something needs it: the module records where icudtl.dat
// sits next to the extension, a caller may record what it wants instead, and the
// first entry point that actually requires V8 calls ensure_initialized(), which
// pays the startup cost once.  Importing the package therefore starts no V8.
class RuntimeManager
{
public:
    RuntimeManager();
    ~RuntimeManager();

    // Configuration for a start that has not happened yet.  Rejected once V8 has
    // started, because startup flags and the ICU data file cannot be changed then.
    bool configure(PyObject* icu, PyObject* params);

    // Where the module found icudtl.dat, for the default start.  Owned by the
    // manager; passing nullptr forgets it.
    void set_default_icu_path(PyObject* path);

    bool initialize(PyObject* icu, PyObject* params);

    // Starts V8 if it has not started yet and reports whether it is running.  Every
    // entry point that needs the runtime goes through here, so lazy startup is
    // decided in one place.  After shutdown_v8() this reports the same "shut down"
    // error the explicit API used to, and never restarts.
    bool ensure_initialized();

    bool shutdown(bool report_error = true);

    bool is_initialized() const;
    bool is_disposed() const { return platform_.is_disposed(); }
    bool pump_foreground_task(v8::Isolate* isolate) {
        return platform_.pump_foreground_task(isolate);
    }
    void notify_isolate_shutdown(v8::Isolate* isolate) {
        platform_.notify_isolate_shutdown(isolate);
    }
    std::shared_ptr<ForegroundNotifications> foreground_notifications() {
        return platform_.foreground_notifications();
    }
    std::uint64_t foreground_token(v8::Isolate* isolate) {
        return platform_.foreground_token(isolate);
    }

    IsolateState* default_isolate() const { return default_isolate_.get(); }
    IsolateState* create_isolate(bool thread_bound = false);
    bool destroy_isolate(IsolateState* state, bool report_error = true);
    IsolateState* find_isolate(v8::Isolate* isolate) const;

private:
    // The path and flags the next start should use: what configure() recorded, or
    // what the module derived from its own location.
    PyObject* pending_icu_path() const;

    PlatformState platform_;
    std::unique_ptr<IsolateState> default_isolate_;
    std::unordered_set<IsolateState*> custom_isolates_;

    PyObject* default_icu_path_ = nullptr;    // owned
    PyObject* configured_icu_path_ = nullptr; // owned
    PyObject* configured_params_ = nullptr;   // owned
    bool initializing_ = false; // guards re-entry; V8 startup happens under the GIL
};

extern RuntimeManager runtime_manager;

#endif
