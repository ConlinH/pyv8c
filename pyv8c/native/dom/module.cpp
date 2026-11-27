#include "dom.h"

namespace {

using namespace pyv8c::dom;

const char* key_names[] = {
    "_services", "_listeners", "_listener_order", "_handlers", "nodeType", "nodeName",
    "nodeValue", "parentNode", "_children", "_child_nodes_view", "__setattr__",
    "_invalidate_tree", "localName", "_raw_text", "_parser_created", "index",
    "append_child", "_adopt_child", "__func__", "__self__", "attributes", "_attribute_map",
    "__getattribute__", "__getattr__", ""
};
PyMethodDef methods[] = {
    {"initialize_target", reinterpret_cast<PyCFunction>(initialize_target), METH_FASTCALL, "Initialize EventTarget storage."},
    {"initialize_node", reinterpret_cast<PyCFunction>(initialize_node), METH_FASTCALL, "Initialize Node storage."},
    {"create_child_nodes_view", reinterpret_cast<PyCFunction>(create_child_nodes_view), METH_FASTCALL, "Create and cache the live child view."},
    {"build_batch", build_batch, METH_VARARGS, "Construct a bounded ordinary HTML token batch."},
    {"build_fragment", build_fragment, METH_VARARGS, "Scan and construct an ordinary fragment batch."},
    {"match_attribute_batch", match_attribute_batch, METH_VARARGS, "Return matching candidate nodes in order."},
    {"create_attribute_projection", create_attribute_projection, METH_VARARGS, "Project ordered attribute values."},
    {"update_attribute_projection", update_attribute_projection, METH_VARARGS, "Refresh one projected row."},
    {"match_attribute_projection", match_attribute_projection, METH_VARARGS, "Match a reusable attribute projection."},
    {nullptr, nullptr, 0, nullptr}
};
int traverse(PyObject* module, visitproc visit, void* arg) {
    auto* state = static_cast<State*>(PyModule_GetState(module));
    if (!state) return 0;
    for (auto* key : state->keys) Py_VISIT(key);
    Py_VISIT(state->view_keywords);
    Py_VISIT(state->element_keywords);
    Py_VISIT(state->text_keywords);
    return 0;
}
int clear(PyObject* module) {
    auto* state = static_cast<State*>(PyModule_GetState(module));
    if (!state) return 0;
    for (auto& key : state->keys) Py_CLEAR(key);
    Py_CLEAR(state->view_keywords);
    Py_CLEAR(state->element_keywords);
    Py_CLEAR(state->text_keywords);
    return 0;
}
void free_module(void* module) { clear(static_cast<PyObject*>(module)); }
PyModuleDef module = {PyModuleDef_HEAD_INIT, "pyv8c._native.dom", nullptr, sizeof(State), methods,
                      nullptr, traverse, clear, free_module};

}  // namespace

extern "C" PyObject* pyv8c_create_dom_module() {
    PyObject* result = PyModule_Create(&module);
    if (!result) return nullptr;
    auto* state = static_cast<State*>(PyModule_GetState(result));
    for (int n = 0; n < KeyCount; ++n) {
        state->keys[n] = PyUnicode_InternFromString(key_names[n]);
        if (!state->keys[n]) { Py_DECREF(result); return nullptr; }
    }
    state->view_keywords = Py_BuildValue("(ss)", "owner", "getter");
    state->element_keywords = Py_BuildValue("(ss)", "services", "attributes");
    state->text_keywords = Py_BuildValue("(s)", "services");
    if (!state->view_keywords || !state->element_keywords || !state->text_keywords) {
        Py_DECREF(result); return nullptr;
    }
    if (result && PyModule_AddStringConstant(result, "SOURCE_SHA256", DOM_SOURCE_SHA256) < 0) {
        Py_DECREF(result);
        return nullptr;
    }
    return result;
}
