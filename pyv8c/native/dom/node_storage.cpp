#include "dom.h"

namespace pyv8c::dom {

namespace {

bool direct_setter(State* state, PyObject* node, PyObject* expected, bool& direct) {
    Ref type(PyObject_Type(node));
    Ref meta(type ? PyObject_Type(type.get()) : nullptr);
    if (!meta) return false;
    if (meta.get() != reinterpret_cast<PyObject*>(&PyType_Type)) {
        direct = false;
        return true;
    }
    Ref setter(type ? PyObject_GetAttr(type.get(), state->keys[Setter]) : nullptr);
    if (!setter) return false;
    direct = setter.get() == expected;
    return true;
}

bool field(State* state, PyObject* node, Key key, PyObject* value, PyObject* expected) {
    bool direct;
    if (!value || !direct_setter(state, node, expected, direct)) return false;
    return (direct ? PyObject_GenericSetAttr(node, state->keys[key], value)
                            : PyObject_SetAttr(node, state->keys[key], value)) == 0;
}

PyObject* children_getter(PyObject* node, PyObject*) {
    return PyObject_GetAttrString(node, "_children");
}
PyMethodDef children_method = {"children", children_getter, METH_NOARGS, nullptr};

PyObject* make_child_nodes_view(State* state, PyObject* node, PyObject* node_list, PyObject* setter) {
    Ref getter(PyCFunction_NewEx(&children_method, node, nullptr));
    PyObject* view_args[] = {node, getter.get()};
    Ref view(getter ? PyObject_Vectorcall(node_list, view_args, 0, state->view_keywords) : nullptr);
    if (!field(state, node, ChildView, view.get(), setter)) return nullptr;
    return Py_NewRef(view.get());
}

}  // namespace

PyObject* initialize_target(PyObject* module, PyObject* const* args, Py_ssize_t size) {
    if (size != 3) {
        PyErr_SetString(PyExc_TypeError, "initialize_target requires 3 arguments");
        return nullptr;
    }
    auto* state = static_cast<State*>(PyModule_GetState(module));
    PyObject *node = args[0], *services = args[1], *setter = args[2];
    if (!field(state, node, Services, services, setter)) return nullptr;
    Ref listeners(PyDict_New());
    Ref zero(PyLong_FromLong(0));
    Ref handlers(PyDict_New());
    if (!field(state, node, Listeners, listeners.get(), setter)
            || !field(state, node, Order, zero.get(), setter)
            || !field(state, node, Handlers, handlers.get(), setter)) return nullptr;
    Py_RETURN_NONE;
}

PyObject* create_child_nodes_view(PyObject* module, PyObject* const* args, Py_ssize_t size) {
    if (size != 3) {
        PyErr_SetString(PyExc_TypeError, "create_child_nodes_view requires 3 arguments");
        return nullptr;
    }
    auto* state = static_cast<State*>(PyModule_GetState(module));
    return make_child_nodes_view(state, args[0], args[1], args[2]);
}

PyObject* initialize_node(PyObject* module, PyObject* const* args, Py_ssize_t size) {
    if (size != 7) {
        PyErr_SetString(PyExc_TypeError, "initialize_node requires 7 arguments");
        return nullptr;
    }
    auto* state = static_cast<State*>(PyModule_GetState(module));
    PyObject *node = args[0], *kind = args[1], *name = args[2], *value = args[3],
             *child_list = args[4], *node_list = args[5], *setter = args[6];
    if (!field(state, node, Kind, kind, setter)
            || !field(state, node, Name, name, setter)
            || !field(state, node, Value, value, setter)
            || !field(state, node, Parent, Py_None, setter)) return nullptr;
    PyObject* child_args[] = {node};
    Ref children(PyTuple_CheckExact(child_list) && PyTuple_Size(child_list) == 0
                 ? Py_NewRef(child_list) : PyObject_Vectorcall(child_list, child_args, 1, nullptr));
    if (!field(state, node, Children, children.get(), setter)) return nullptr;
    bool ordinary;
    if (!direct_setter(state, node, setter, ordinary)) return nullptr;
    if (ordinary) {
        Ref type(PyObject_Type(node));
        if (!type) return nullptr;
        Ref view(PyObject_GetAttr(type.get(), state->keys[ChildView]));
        if (view.get() == Py_None) Py_RETURN_NONE;
        if (!view) {
            if (!PyErr_ExceptionMatches(PyExc_AttributeError)) return nullptr;
            PyErr_Clear();
        }
    }
    // A Python subclass may observe the original initialization assignment.
    Ref view(make_child_nodes_view(state, node, node_list, setter));
    if (!view) return nullptr;
    Py_RETURN_NONE;
}

}  // namespace pyv8c::dom
