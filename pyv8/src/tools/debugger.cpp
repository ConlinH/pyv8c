#include "bridge_base.h"
#include <Python.h>
#include "entry.h"
#include "context.h"
#include "debugger.h"
#include "microtask_queue.h"
#include "watchdog.h"
#include <v8-inspector.h>
#include <algorithm>
#include <cmath>
#include <map>
#include <memory>
#include <string>
#include <vector>

using namespace v8;
using namespace v8_inspector;
struct InspectorHub;

static void call_callback(debugger_c* self, const char* name) {
    GILAcquireScope acquire;
    PyObject* callback = PyObject_GetAttrString((PyObject*)self, name);
    if (!callback) { PyErr_Clear(); return; }
    PyObject* result = PyObject_CallNoArgs(callback);
    Py_DECREF((PyObject*)callback);
    if (!result) PyErr_WriteUnraisable((PyObject*)self);
    else Py_DECREF((PyObject*)result);
}
struct InspectorChannel : V8Inspector::Channel {
    debugger_c* owner;
    bool enabled = true;
    explicit InspectorChannel(debugger_c* value) : owner(value) {}
    void deliver(std::unique_ptr<StringBuffer> message) {
        if (!enabled || !message) return;
        GILAcquireScope acquire;
        StringView view = message->string();
        PyObject* text = view.is8Bit()
            ? PyUnicode_DecodeLatin1((const char*)view.characters8(), view.length(), nullptr)
            : PyUnicode_DecodeUTF16((const char*)view.characters16(), view.length() * 2, nullptr, nullptr);
        if (!text) { PyErr_WriteUnraisable((PyObject*)owner); return; }
        PyObject* callback = PyObject_GetAttrString((PyObject*)owner, "handle");
        if (callback) {
            PyObject* result = PyObject_CallFunctionObjArgs(callback, text, nullptr);
            Py_DECREF((PyObject*)callback);
            if (!result) PyErr_WriteUnraisable((PyObject*)owner);
            else Py_DECREF((PyObject*)result);
        } else PyErr_Clear();
        Py_DECREF((PyObject*)text);
    }
    void sendResponse(int, std::unique_ptr<StringBuffer> message) override { deliver(std::move(message)); }
    void sendNotification(std::unique_ptr<StringBuffer> message) override { deliver(std::move(message)); }
    void flushProtocolNotifications() override {}
};
struct DebuggerState {
    std::shared_ptr<InspectorHub> hub;
    std::unique_ptr<InspectorChannel> channel;
    std::unique_ptr<V8InspectorSession> session;
    int group = 0;
    bool run_requested = false;
    bool pending_pause = false;
    std::string pause_reason;
};
// Mirror Blink's MemoryInfo IDL object: three native heap getters live on the
// prototype, which also carries Symbol.toStringTag == "MemoryInfo" (and no own
// constructor), so String(console.memory) yields "[object MemoryInfo]".
static void memory_info_getter(const FunctionCallbackInfo<Value>& info) {
    Isolate* isolate = info.GetIsolate();
    HeapStatistics stats;
    isolate->GetHeapStatistics(&stats);
    int field = info.Data().As<Int32>()->Value();
    double raw = field == 0 ? stats.heap_size_limit()
                : field == 1 ? stats.total_heap_size()
                             : stats.used_heap_size();
    constexpr double bucket = 1024.0 * 1024 * 10;
    info.GetReturnValue().Set(Number::New(isolate, std::ceil(raw / bucket) * bucket));
}

// Build one console.memory object: a plain instance inheriting from a prototype
// that owns the three heap accessors and the MemoryInfo tag, as Blink does.
static MaybeLocal<Object> build_memory_info(Isolate* isolate, Local<Context> context) {
    Local<Object> proto = Object::New(isolate);
    auto add_heap_getter = [&](const char* name, int field) {
        Local<FunctionTemplate> tpl = FunctionTemplate::New(
            isolate, memory_info_getter, Int32::New(isolate, field));
        Local<Function> fn = tpl->GetFunction(context).ToLocalChecked();
        fn->SetName(String::NewFromUtf8(
            isolate, (std::string("get ") + name).c_str()).ToLocalChecked());
        PropertyDescriptor descriptor(fn, Undefined(isolate));
        descriptor.set_enumerable(true);
        descriptor.set_configurable(true);
        proto->DefineProperty(context,
            String::NewFromUtf8(isolate, name).ToLocalChecked(), descriptor);
    };
    add_heap_getter("jsHeapSizeLimit", 0);
    add_heap_getter("totalJSHeapSize", 1);
    add_heap_getter("usedJSHeapSize", 2);
    proto->DefineOwnProperty(context, Symbol::GetToStringTag(isolate),
        String::NewFromUtf8Literal(isolate, "MemoryInfo"),
        static_cast<PropertyAttribute>(ReadOnly | DontEnum));
    Local<Object> info = Object::New(isolate);
    if (info->SetPrototype(context, proto).IsNothing())
        return MaybeLocal<Object>();
    return info;
}

struct InspectorHub : V8InspectorClient {
    Isolate* isolate;
    std::unique_ptr<V8Inspector> inspector;
    std::map<context_c*, int> groups;
    std::vector<debugger_c*> sessions;
    std::vector<debugger_c*> dispatch_stack;
    std::vector<debugger_c*> pause_stack;
    int next_group = 1;
    explicit InspectorHub(Isolate* value) : isolate(value) {
        inspector = V8Inspector::create(isolate, this);
    }
    MaybeLocal<Value> memoryInfo(Isolate*, Local<Context> context) override {
        EscapableHandleScope scope(isolate);
        MaybeLocal<Object> info = build_memory_info(isolate, context);
        if (info.IsEmpty()) return MaybeLocal<Value>();
        return scope.Escape(info.ToLocalChecked());
    }
    // Register a realm with the inspector.  The console asks V8 to expose
    // MemoryInfo on every context, so the flag belongs here rather than being
    // repeated at each of the call sites.
    void context_created(context_c* context, int group) {
        V8ContextInfo info(context->js_context.Get(isolate), group, StringView());
        info.hasMemoryOnConsole = true;
        inspector->contextCreated(info);
    }
    Local<Context> ensureDefaultContextInGroup(int group) override {
        for (auto& entry : groups)
            if (entry.second == group) return entry.first->js_context.Get(isolate);
        return Local<Context>();
    }
    void runIfWaitingForDebugger(int) override {
        if (!dispatch_stack.empty()) dispatch_stack.back()->state->run_requested = true;
    }
    void runMessageLoopOnPause(int group) override {
        auto found = std::find_if(sessions.begin(), sessions.end(), [group](debugger_c* entry) {
            return entry->state->group == group;
        });
        if (found == sessions.end()) return;
        debugger_c* owner = *found;
        Py_INCREF((PyObject*)owner);
        pause_stack.push_back(owner);
        auto& watchdog = owner->context->isolate_state->watchdog();
        auto suspended = watchdog.suspend_deadlines();
        call_callback(owner, "run_loop");
        watchdog.resume_deadlines(suspended);
        pause_stack.pop_back();
        Py_DECREF((PyObject*)owner);
    }
    void quitMessageLoopOnPause() override {
        if (!pause_stack.empty()) call_callback(pause_stack.back(), "quit_loop");
    }
};
static std::map<Isolate*, std::weak_ptr<InspectorHub>> inspector_hubs;
static std::shared_ptr<InspectorHub> hub_for(Isolate* isolate) {
    auto hub = inspector_hubs[isolate].lock();
    if (!hub) {
        hub = std::make_shared<InspectorHub>(isolate);
        inspector_hubs[isolate] = hub;
    }
    return hub;
}
PyObject* context_enable_console(context_c* context, PyObject*) {
    if (!context_is_active(context)) {
        PyErr_SetString(PyExc_RuntimeError, "Context is closed");
        return nullptr;
    }
    IN_ISOLATE(context->isolate_state);
    auto hub = hub_for(isolate);
    if (!hub->groups.count(context)) {
        int group = hub->next_group++;
        hub->groups[context] = group;
        hub->context_created(context, group);
    }
    if (!context->resources) context->resources = new ContextResources();
    context->resources->console_inspector = hub;
    PYV8_RETURN_NONE;
}
static PyObject* debugger_new(PyTypeObject* type, PyObject*, PyObject*) {
    return PyType_GenericAlloc(type, 0);
}
static int debugger_init(debugger_c* self, PyObject* args, PyObject* kwargs) {
    PyObject* context_object;
    static const char* keywords[] = {"context", nullptr};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "O:Debugger", const_cast<char**>(keywords), &context_object)) return -1;
    if (self->state) { PyErr_SetString(PyExc_RuntimeError, "Debugger is already initialized"); return -1; }
    if (!PyObject_TypeCheck(context_object, context_type)) {
        PyErr_SetString(PyExc_TypeError, "context must be an active Context"); return -1;
    }
    context_c* context = (context_c*)context_object;
    if (!context->isolate_state || context->js_context.IsEmpty()) {
        PyErr_SetString(PyExc_RuntimeError, "Context is closed"); return -1;
    }
    IN_ISOLATE(context->isolate_state);
    auto state = std::make_unique<DebuggerState>();
    state->hub = hub_for(isolate);
    auto& hub = *state->hub;
    auto found = hub.groups.find(context);
    if (found == hub.groups.end()) {
        state->group = hub.next_group++;
        hub.groups[context] = state->group;
        hub.context_created(context, state->group);
    } else state->group = found->second;
    state->channel = std::make_unique<InspectorChannel>(self);
    state->session = hub.inspector->connect(state->group, state->channel.get(), StringView(), V8Inspector::kFullyTrusted);
    Py_INCREF((PyObject*)context_object);
    self->context = context;
    self->state = state.release();
    hub.sessions.push_back(self);
    return 0;
}
static bool require_session(debugger_c* self) {
    if (self->state && self->state->session && self->context && self->context->isolate_state && !self->context->js_context.IsEmpty()) return true;
    PyErr_SetString(PyExc_RuntimeError, "Debugger is closed");
    return false;
}
static PyObject* debugger_rebind_context(debugger_c* self, PyObject* object) {
    if (!require_session(self)) return nullptr;
    if (!PyObject_TypeCheck(object, context_type)) {
        PyErr_SetString(PyExc_TypeError, "context must be an active Context"); return nullptr;
    }
    auto* next = (context_c*)object;
    auto* previous = self->context;
    if (next == previous) PYV8_RETURN_NONE;
    if (!next->isolate_state || next->isolate_state != previous->isolate_state || next->js_context.IsEmpty()) {
        PyErr_SetString(PyExc_ValueError, "replacement context must belong to the same isolate"); return nullptr;
    }
    if (!microtask_queue_check(next->microtask_queue) || !microtask_queue_check(previous->microtask_queue)) return nullptr;
    PyOwnedRef previous_owner(PyV8_NewRef((PyObject*)previous));
    IN_ISOLATE(next->isolate_state);
    auto hub = self->state->hub;
    bool next_has_session = std::any_of(hub->sessions.begin(), hub->sessions.end(),
                                      [next](debugger_c* item) { return item->context == next; });
    if (!hub->pause_stack.empty() || next_has_session) {
        PyErr_SetString(PyExc_RuntimeError, "cannot replace a paused or already registered context"); return nullptr;
    }
    if (hub->groups.count(next)) {
        hub->inspector->contextDestroyed(next->js_context.Get(isolate));
        hub->groups.erase(next);
    }
    const int group = self->state->group;
    hub->inspector->contextDestroyed(previous->js_context.Get(isolate));
    hub->groups.erase(previous);
    hub->groups[next] = group;
    for (auto* session : hub->sessions) {
        if (session->context != previous) continue;
        Py_INCREF(object);
        session->context = next;
        Py_DECREF((PyObject*)previous);
    }
    hub->context_created(next, group);
    PYV8_RETURN_NONE;
}
static PyObject* debugger_send(debugger_c* self, PyObject* message) {
    if (!require_session(self)) return nullptr;
    char* bytes;
    Py_ssize_t length;
    if (PyBytes_AsStringAndSize(message, &bytes, &length) < 0) return nullptr;
    IN_ISOLATE(self->context->isolate_state);
    IN_CONTEXT(self->context->js_context.Get(isolate));
    auto hub = self->state->hub;
    hub->dispatch_stack.push_back(self);
    self->state->session->dispatchProtocolMessage(StringView((const uint8_t*)bytes, length));
    hub->dispatch_stack.pop_back();
    if (self->state->run_requested) {
        self->state->run_requested = false;
        call_callback(self, "on_ready");
    }
    PYV8_RETURN_NONE;
}
static PyObject* debugger_schedule_pause(debugger_c* self, PyObject* args, PyObject* kwargs) {
    const char* reason = "pause on next statement";
    static const char* keywords[] = {"reason", nullptr};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "|s", const_cast<char**>(keywords), &reason)) return nullptr;
    if (!require_session(self)) return nullptr;
    self->state->pending_pause = true;
    self->state->pause_reason = reason;
    PYV8_RETURN_NONE;
}
static PyObject* debugger_cancel_pause(debugger_c* self, PyObject*) {
    if (!self->state || !self->state->session) PYV8_RETURN_NONE;
    if (!require_session(self)) return nullptr;
    IN_ISOLATE(self->context->isolate_state);
    self->state->pending_pause = false;
    self->state->session->cancelPauseOnNextStatement();
    PYV8_RETURN_NONE;
}
static PyObject* debugger_resume(debugger_c* self, PyObject*) {
    if (!self->state || !self->state->session) PYV8_RETURN_NONE;
    if (!require_session(self)) return nullptr;
    IN_ISOLATE(self->context->isolate_state);
    self->state->pending_pause = false;
    self->state->session->resume();
    PYV8_RETURN_NONE;
}
static PyObject* debugger_isolate_id(debugger_c* self, void*) {
    if (!require_session(self)) return nullptr;
    return PyLong_FromVoidPtr(self->state->hub->isolate);
}
static PyObject* debugger_disconnect(debugger_c* self, PyObject*) {
    if (!self->state || !self->state->session) PYV8_RETURN_NONE;
    IN_ISOLATE(self->context->isolate_state);
    auto hub = self->state->hub;
    if (!hub->pause_stack.empty() || !hub->dispatch_stack.empty()) {
        PyErr_SetString(PyExc_RuntimeError, "Cannot disconnect Inspector inside an active protocol or pause callback; resume and close on the host thread");
        return nullptr;
    }
    self->state->pending_pause = false;
    self->state->session.reset();
    auto& sessions = hub->sessions;
    sessions.erase(std::remove(sessions.begin(), sessions.end(), self), sessions.end());
    bool remaining = std::any_of(sessions.begin(), sessions.end(), [self](debugger_c* other) { return other->context == self->context; });
    if (!remaining && (!self->context->resources ||
                       !self->context->resources->console_inspector)) {
        hub->inspector->contextDestroyed(self->context->js_context.Get(isolate));
        hub->groups.erase(self->context);
    }
    self->state->hub.reset();
    if (sessions.empty() && hub->groups.empty()) inspector_hubs.erase(isolate);
    PYV8_RETURN_NONE;
}
bool debugger_context_can_close(context_c* context) {
    if (!context->isolate_state) return true;
    IN_ISOLATE(context->isolate_state);
    auto found = inspector_hubs.find(isolate);
    if (found == inspector_hubs.end()) return true;
    auto hub = found->second.lock();
    if (hub && (!hub->pause_stack.empty() || !hub->dispatch_stack.empty())) {
        PyErr_SetString(PyExc_RuntimeError, "Cannot close Context inside an active Inspector callback");
        return false;
    }
    return true;
}
void debugger_isolate_closed(Isolate* isolate) {
    // The registry is keyed by address and lives as long as the process, so an
    // entry must not outlive its isolate: the inspector belongs to this one and
    // has to go before Dispose().
    auto found = inspector_hubs.find(isolate);
    if (found == inspector_hubs.end()) return;
    auto hub = found->second.lock();
    if (hub) {
        auto sessions = hub->sessions;
        for (auto session : sessions) {
            if (session->state == nullptr) continue;
            session->state->pending_pause = false;
            // Dropping both keeps the inspector from being used after the isolate;
            // the Debugger then answers "Debugger is closed".
            session->state->session.reset();
            session->state->hub.reset();
        }
        hub->sessions.clear();
        hub->groups.clear();
        hub->inspector.reset();
    }
    inspector_hubs.erase(isolate);
}
void debugger_context_closed(context_c* context) {
    if (!context->isolate_state) return;
    IN_ISOLATE(context->isolate_state);
    auto found = inspector_hubs.find(isolate);
    if (found == inspector_hubs.end()) return;
    auto hub = found->second.lock();
    if (!hub) return;
    auto sessions = hub->sessions;
    for (auto session : sessions) {
        if (session->context != context) continue;
        PyObject* result = debugger_disconnect(session, nullptr);
        Py_XDECREF((PyObject*)result);
    }
    if (hub->groups.count(context)) {
        hub->inspector->contextDestroyed(context->js_context.Get(isolate));
        hub->groups.erase(context);
    }
    if (context->resources) context->resources->console_inspector.reset();
    if (hub->groups.empty() && hub->sessions.empty()) inspector_hubs.erase(isolate);
}
void debugger_prepare_script(context_c* context) {
    auto found = inspector_hubs.find(context->isolate_state->isolate());
    if (found == inspector_hubs.end()) return;
    auto hub = found->second.lock();
    if (!hub || !hub->dispatch_stack.empty()) return;
    for (auto debugger : hub->sessions) {
        auto state = debugger->state;
        if (debugger->context != context || !state->pending_pause) continue;
        state->pending_pause = false;
        StringView reason((const uint8_t*)state->pause_reason.data(), state->pause_reason.size());
        state->session->schedulePauseOnNextStatement(reason, StringView());
    }
}
static void debugger_dealloc(debugger_c* self) {
    if (self->state) {
        self->state->channel->enabled = false;
        PyObject* result = debugger_disconnect(self, nullptr);
        Py_XDECREF((PyObject*)result);
        delete self->state;
    }
    Py_XDECREF((PyObject*)self->context);
    TP_FREE_ABI3(debugger_type, self);
}
static PyMethodDef debugger_methods[] = {
    {"rebind_context", (PyCFunction)PythonBoundary<debugger_rebind_context>::call, METH_O, nullptr},
    {"send", (PyCFunction)PythonBoundary<debugger_send>::call, METH_O, nullptr},
    {"schedule_pause_on_next_statement", (PyCFunction)PythonBoundary<debugger_schedule_pause>::call, METH_VARARGS | METH_KEYWORDS, nullptr},
    {"cancel_pause_on_next_statement", (PyCFunction)PythonBoundary<debugger_cancel_pause>::call, METH_NOARGS, nullptr},
    {"resume", (PyCFunction)PythonBoundary<debugger_resume>::call, METH_NOARGS, nullptr},
    {"disconnect", (PyCFunction)PythonBoundary<debugger_disconnect>::call, METH_NOARGS, nullptr},
    {nullptr}
};
static PyGetSetDef debugger_getsets[] = {
    {"isolate_id", (getter)PythonBoundary<debugger_isolate_id>::call, nullptr, nullptr, nullptr},
    {nullptr}
};
PyTypeObject* debugger_type = nullptr;
static PyType_Slot debugger_slots[] = {
    {Py_tp_dealloc, (void*)PythonBoundary<debugger_dealloc>::call},
    {Py_tp_new, (void*)PythonBoundary<debugger_new>::call},
    {Py_tp_init, (void*)debugger_init},
    {Py_tp_methods, debugger_methods},
    {Py_tp_getset, debugger_getsets},
    {0, nullptr}
};
static PyType_Spec debugger_spec = {
    "pyv8c.pyv8.Debugger", sizeof(debugger_c), 0,
    Py_TPFLAGS_DEFAULT | Py_TPFLAGS_BASETYPE, debugger_slots
};
int debugger_type_init() {
    debugger_type = (PyTypeObject*)PyType_FromSpec(&debugger_spec);
    return debugger_type ? 0 : -1;
}
