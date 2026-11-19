#ifndef DEBUGGER_H
#define DEBUGGER_H
#include <Python.h>
#include "context.h"
struct DebuggerState;
typedef struct _debugger {
    PyObject_HEAD
    context_c* context;
    DebuggerState* state;
} debugger_c;
extern PyTypeObject* debugger_type;
int debugger_type_init();
PyObject* context_enable_console(context_c*, PyObject*);
void debugger_prepare_script(context_c* context);
void debugger_context_closed(context_c* context);
bool debugger_context_can_close(context_c* context);
// Called before the isolate is disposed -- the last moment an inspector built on
// it can be taken apart.  See the note on the definition.
void debugger_isolate_closed(v8::Isolate* isolate);
#endif
