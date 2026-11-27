#pragma once

// The DOM kernel uses only CPython's stable ABI.
#include <Python.h>

namespace pyv8c::dom {

enum Key {
    Services, Listeners, Order, Handlers, Kind, Name, Value, Parent, Children,
    ChildView, Setter, Invalidate, LocalName, RawText, ParserCreated, Index,
    Append, Adopt, Function, Self, Attributes, AttributeMap, GetAttribute, GetAttr,
    Empty, KeyCount
};
struct State {
    PyObject* keys[KeyCount];
    PyObject* view_keywords;
    PyObject* element_keywords;
    PyObject* text_keywords;
};

class Ref {
    PyObject* value_;
public:
    explicit Ref(PyObject* value = nullptr) : value_(value) {}
    ~Ref() { Py_XDECREF(value_); }
    Ref(const Ref&) = delete;
    Ref& operator=(const Ref&) = delete;
    PyObject* get() const { return value_; }
    explicit operator bool() const { return value_ != nullptr; }
};

inline bool word(PyObject* value, const char* expected) {
    return PyUnicode_Check(value) && PyUnicode_CompareWithASCIIString(value, expected) == 0;
}

PyObject* initialize_target(PyObject*, PyObject* const*, Py_ssize_t);
PyObject* initialize_node(PyObject*, PyObject* const*, Py_ssize_t);
PyObject* create_child_nodes_view(PyObject*, PyObject* const*, Py_ssize_t);
PyObject* build_batch(PyObject*, PyObject*);
PyObject* build_fragment(PyObject*, PyObject*);
PyObject* match_attribute_batch(PyObject*, PyObject*);
PyObject* create_attribute_projection(PyObject*, PyObject*);
PyObject* update_attribute_projection(PyObject*, PyObject*);
PyObject* match_attribute_projection(PyObject*, PyObject*);

}  // namespace pyv8c::dom
