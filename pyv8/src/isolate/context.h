#ifndef CONTEXT_H
#define CONTEXT_H


#include "bridge_base.h"
#include <Python.h>
#include <v8.h>
#include <unordered_map>
#include <unordered_set>
#include <memory>
#include <string>
#include <vector>

#include "v8_handle_slot.h"

class IsolateState;
class WasmStreamSession;
struct context_c;
enum class ContextState { Active, Closing, Closed };
struct ContextLifetime {
    context_c* owner;

    // The pair table: a Python host object -> the JavaScript object standing for
    // it in this realm.  The table owns neither side; an entry goes when its
    // owner is released.  It lives here, not on the context, so a handle that
    // outlives the context reads an empty table.
    std::unordered_map<PyObject*, PythonObjectOwner*> js_object_cache;

    explicit ContextLifetime(context_c* owner) : owner(owner) {}
};

struct ContextResources {
    std::size_t handle_count() const { return handles.size(); }
    std::vector<std::shared_ptr<WasmStreamSession>> wasm_streams;
    std::shared_ptr<void> console_inspector;
private:
    friend class IsolateState;
    friend PyObject* context_new(PyTypeObject*, PyObject*, PyObject*);
    friend void context_finish_close(context_c*);
    friend PyObject* context_promise_rejection_state(context_c*, PyObject*);
    friend void context_register_handle(context_c*, PyObject*, int (*)(PyObject*));
    friend void context_unregister_handle(context_c*, PyObject*);
    friend bool context_retain_dependency(v8::Local<v8::Context>, PyObject*);
    void drop_dependency(context_c* owner, context_c* origin);
    void unlink_dependencies(context_c* owner);
    V8HandleSlot<v8::Private> rejection_key;
    uint64_t next_rejection_id = 0;
    std::shared_ptr<ContextLifetime> lifetime;
    std::unordered_set<PythonObjectOwner*> python_owners;
    std::unordered_set<void*> callbacks;
    std::unordered_map<PyObject*, int (*)(PyObject*)> handles;
    std::unordered_set<context_c*> dependents;
    std::unordered_set<context_c*> dependencies;
    // Where each dependency sits in `owner->dependencies`, so removing one is a
    // swap instead of a scan of the list.
    std::unordered_map<context_c*, Py_ssize_t> dependency_slots;
};

struct PythonObjectOwner;

struct TraceSourceFilter {
    std::vector<std::string> patterns;
    std::unordered_map<std::string, bool> matches;
};

// Every Python reference a realm owns.  The struct, the traversal and the close
// walk all read this one list, so a field cannot be added without being visited.
#define CONTEXT_PY_FIELDS(X) \
    X(py_isolate)            \
    X(microtask_queue)       \
    X(registry)              \
    X(function_template_cache) \
    X(callback_targets)      \
    X(lazy_factory)          \
    X(global_template)       \
    X(py_global)             \
    X(trace_callback)        \
    X(trace_context_type)    \
    X(trace_event_names)     \
    X(uncaught_handler)      \
    X(rejection_handler)     \
    X(code_generation_handler) \
    X(dependencies)

typedef struct context_c
{
    PyObject_HEAD

    IsolateState* isolate_state;
    PyObject* py_isolate;
    PyObject* microtask_queue;
    V8HandleSlot<v8::Context> js_context;
    //: What this realm knows about each interface name it has met: name -> entry.
    PyObject* registry;
    PyObject* function_template_cache;
    PyObject* callback_targets;
    PyObject* uncaught_handler;
    PyObject* rejection_handler;
    PyObject* code_generation_handler;
    PyObject* dependencies;
    ContextResources* resources;
    ContextState state;
    context_c* next_pending_close;
    // Optional callable used to materialise a class the realm holds no config for.
    // Called with an interface name, it answers the Python class, or (class, specs).
    PyObject* lazy_factory;
    //: The global class's template, kept for the context-dependent member phase.
    PyObject* global_template;
    PyObject* py_global;
    bool has_done;
    // Global instance members are installed explicitly in the caller's order.
    bool ordered_global;
    //: Left behind by a navigation; see ``context_is_detached``.
    bool detached;
    double timeout;
    bool trace;
    bool traceable;
    unsigned int trace_depth;
    TraceSourceFilter* trace_source_filter;

    PyObject* trace_callback;
    PyObject* trace_context_type;
    PyObject* trace_event_names;
} context_c;

// A realm's template caches own their entries for as long as the realm lives; a
// lookup hands the caller a reference of its own, which it releases as soon as
// it is done.  Returning the cached reference itself left callers unsure
// whether releasing it was allowed.
inline PyObject* owned_template(PyObject* templ)
{
    Py_XINCREF(templ);
    return templ;
}

// A realm keeps one entry per interface name it has met: the template it built,
// the spec that built it, and where the name is in the ask-the-factory protocol.
enum RegistrySlot { REGISTRY_TEMPLATE, REGISTRY_SPEC, REGISTRY_STATE, REGISTRY_SHARED, REGISTRY_SLOTS };
enum RegistryState { REGISTRY_IDLE, REGISTRY_RESOLVING, REGISTRY_ASKED };

//: What one name holds in one slot, or NULL when the name has no entry yet.
PyObject* context_registry_get(context_c* self, PyObject* name, int slot);
//: Write one slot of one name, creating the entry if this is the first fact about it.
int context_registry_set(context_c* self, PyObject* name, int slot, PyObject* value);
//: Whether the realm may ask its factory about this name, once, right now.
bool context_registry_askable(context_c* self, PyObject* name);
//: Ask about one name, keeping the specs the answer carries.  Returns the class,
//: Py_None when the realm has no answer, or NULL with an error set.
PyObject* context_ask_factory(context_c* self, PyObject* name);

// Realm composition owns the interface callbacks and registered constructors.
void context_install_access_check(v8::Isolate* isolate);
v8::Local<v8::Function> context_own_constructor(v8::Local<v8::Context> context, const char* name);

bool context_is_active(context_c* self);
context_c* context_from_v8(v8::Local<v8::Context> context);

//: Whether a navigation has replaced this realm's document.  Blink's detached global
//: object: still alive, but answering "less things" (``window_proxy.h``).
bool context_is_detached(v8::Local<v8::Context> context);
//: Mark a realm as left behind (``context_is_detached``).
PyObject* context_detach(context_c* self, PyObject* ignored);
bool context_check_callback(v8::Local<v8::Context> context);
PyObject* context_set_code_generation_handler(context_c*, PyObject*);
v8::ModifyCodeGenerationFromStringsResult context_modify_code_generation(
    v8::Local<v8::Context>, v8::Local<v8::Value>, bool);
void context_finish_close(context_c* self);
void context_register_handle(context_c* self, PyObject* handle, int (*clear)(PyObject*));
void context_unregister_handle(context_c* self, PyObject* handle);
bool context_retain_dependency(v8::Local<v8::Context> target, PyObject* source);

bool context_trace_source_matches(context_c* context);
inline bool context_trace_enabled(context_c* context) {
    return context != nullptr && context->trace && context->trace_callback != nullptr &&
           context->trace_depth == 0 && context_trace_source_matches(context);
}

extern PyTypeObject* context_type;
int context_type_init();
PyObject* context_get_trace(context_c* self, void* closure);
int context_set_trace(context_c* self, PyObject* value, void* closure);

void context_dealloc(context_c* self);
PyObject* context_new(PyTypeObject* type, PyObject* args, PyObject* kwargs);
PyObject* context_run(context_c* self, PyObject* args, PyObject* kwargs);
PyObject* context_expose(context_c* self, PyObject* args, PyObject* kwargs);
PyObject* context_hide_global(context_c* self, PyObject* name);
PyObject* context_js_obj(context_c* self, PyObject* py_object);
PyObject* context_install(context_c* self, PyObject* names);
PyObject* context_install_ordered(context_c* self, PyObject* names);
PyObject* context_define_global(context_c* self, PyObject* args, PyObject* kwargs);
PyObject* context_gc(context_c* self);
PyObject* context_close(context_c* self, PyObject* ignored);
PyObject* context_enqueue_microtask(context_c* self, PyObject* function);
PyObject* context_set_error_hooks(context_c* self, PyObject* args, PyObject* kwargs);

// Embedder data slots
#define CONTEXT_OBJECT_SLOT 1
#define OBJECT_PROTOTYPE_SLOT 2
#define CTX_TYPE 4

PyObject* context_get_global(context_c* self, void* ignored);

PyObject* context_get_timeout(context_c* self, void* ignored);
int context_set_timeout(context_c* self, PyObject* value, void* ignored);

PyObject* context_getattro(context_c* self, PyObject* name);
PyObject* context_getitem(context_c* self, PyObject* name);
int context_setattro(context_c* self, PyObject* name, PyObject* value);
int context_setitem(context_c* self, PyObject* name, PyObject* value);


#endif
