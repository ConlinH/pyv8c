// CPython's stable ABI is sufficient here; the DOM kernel has no V8 dependency.
#include <Python.h>

namespace {
enum Key {
    Services, Listeners, Order, Handlers, Kind, Name, Value, Parent, Children,
    ChildView, Setter, Invalidate, LocalName, RawText, ParserCreated, Index,
    Append, Adopt, Function, Self, KeyCount
};
const char* key_names[] = {
    "_services", "_listeners", "_listener_order", "_handlers", "nodeType", "nodeName",
    "nodeValue", "parentNode", "_children", "_child_nodes_view", "__setattr__",
    "_invalidate_tree", "localName", "_raw_text", "_parser_created", "index",
    "append_child", "_adopt_child", "__func__", "__self__"
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

bool word(PyObject* value, const char* expected) {
    return PyUnicode_Check(value) && PyUnicode_CompareWithASCIIString(value, expected) == 0;
}

bool sequence(PyObject* value, Py_ssize_t size) {
    if (PyTuple_Check(value) && PyTuple_Size(value) == size) return true;
    PyErr_SetString(PyExc_TypeError, "invalid DOM construction token");
    return false;
}

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

PyObject* children_getter(PyObject* node, PyObject*) {
    return PyObject_GetAttrString(node, "_children");
}
PyMethodDef children_method = {"children", children_getter, METH_NOARGS, nullptr};

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
    Ref children(PyObject_Vectorcall(child_list, child_args, 1, nullptr));
    if (!field(state, node, Children, children.get(), setter)) return nullptr;
    Ref getter(PyCFunction_NewEx(&children_method, node, nullptr));
    PyObject* view_args[] = {node, getter.get()};
    Ref view(getter ? PyObject_Vectorcall(node_list, view_args, 0, state->view_keywords) : nullptr);
    if (!field(state, node, ChildView, view.get(), setter)) return nullptr;
    Py_RETURN_NONE;
}

// Only fresh, ordinary parser nodes reach this operation. There is no adoption,
// removal, observer delivery, resource discovery or user hook to replay here.
bool ordinary_method(State* state, PyObject* method, PyObject* parent,
                     PyObject* function, PyObject* method_type, bool& ordinary) {
    Ref type(PyObject_Type(method));
    if (!type) return false;
    ordinary = false;
    if (type.get() != method_type) return true;
    Ref actual(PyObject_GetAttr(method, state->keys[Function]));
    Ref owner(PyObject_GetAttr(method, state->keys[Self]));
    if (!actual || !owner) return false;
    ordinary = actual.get() == function && owner.get() == parent;
    return true;
}

bool append(State* state, PyObject* stack, PyObject* node, PyObject* methods) {
    PyObject* borrowed = PyList_GetItem(stack, PyList_Size(stack) - 1);
    if (!borrowed) return false;
    Ref parent(Py_NewRef(borrowed));
    Ref insertion(PyObject_GetAttr(parent.get(), state->keys[Append]));
    if (!insertion) return false;
    bool ordinary = false;
    PyObject* method_type = PyTuple_GetItem(methods, 2);
    if (!ordinary_method(state, insertion.get(), parent.get(),
                         PyTuple_GetItem(methods, 0), method_type, ordinary)) return false;
    if (ordinary) {
        Ref adoption(PyObject_GetAttr(parent.get(), state->keys[Adopt]));
        if (!adoption || !ordinary_method(state, adoption.get(), parent.get(),
                PyTuple_GetItem(methods, 1), method_type, ordinary)) return false;
    }
    if (!ordinary) {
        Ref result(PyObject_CallFunctionObjArgs(insertion.get(), node, nullptr));
        return static_cast<bool>(result);
    }
    Ref old_parent(PyObject_GetAttr(node, state->keys[Parent]));
    Ref subtree(PyObject_GetAttr(node, state->keys[Children]));
    Ref hook(PyObject_GetAttrString(node, "_on_tree_inserted"));
    Ref shadow(PyObject_GetAttrString(node, "_shadow_root"));
    Ref node_services(PyObject_GetAttr(node, state->keys[Services]));
    Ref parent_services(PyObject_GetAttr(parent.get(), state->keys[Services]));
    if (!old_parent || !subtree || !hook || !shadow || !node_services || !parent_services) return false;
    if (old_parent.get() != Py_None || !PyList_Check(subtree.get())
            || PyList_Size(subtree.get()) != 0 || hook.get() != Py_None
            || shadow.get() != Py_None || node_services.get() != parent_services.get()) {
        Ref result(PyObject_CallFunctionObjArgs(insertion.get(), node, nullptr));
        return static_cast<bool>(result);
    }
    PyObject* invalidate_args[] = {parent.get()};
    Ref result(PyObject_VectorcallMethod(state->keys[Invalidate], invalidate_args, 1, nullptr));
    if (!result) return false;
    Ref children(PyObject_GetAttr(parent.get(), state->keys[Children]));
    if (!children || !PyList_Check(children.get())) {
        if (!PyErr_Occurred()) PyErr_SetString(PyExc_TypeError, "DOM children must be a list");
        return false;
    }
    return PyObject_SetAttr(node, state->keys[Parent], parent.get()) == 0
        && PyList_Append(children.get(), node) == 0;
}

bool consume(State* state, PyObject* tokens, PyObject* parser, Py_ssize_t index) {
    Ref next(PyLong_FromSsize_t(index + 1));
    return next && PyList_SetItem(tokens, index, Py_NewRef(Py_None)) == 0
        && PyObject_SetAttr(parser, state->keys[Index], next.get()) == 0;
}

PyObject* build(PyObject* module, PyObject* args) {
    auto* state = static_cast<State*>(PyModule_GetState(module));
    PyObject *tokens, *stack, *services, *classes, *voids, *closers, *text_type,
             *decode, *element_init, *methods, *parser;
    Py_ssize_t index;
    if (!PyArg_ParseTuple(args, "OnOOOOOOOOOO", &tokens, &index, &stack, &services,
                          &classes, &voids, &closers, &text_type, &decode, &element_init,
                          &methods, &parser)) return nullptr;
    if (!sequence(methods, 3)) return nullptr;
    if (!PyList_Check(tokens) || !PyList_Check(stack) || PyList_Size(stack) == 0
            || !PyDict_Check(classes) || !PyDict_Check(closers)) {
        PyErr_SetString(PyExc_TypeError, "invalid DOM construction state");
        return nullptr;
    }
    const Py_ssize_t length = PyList_Size(tokens);
    if (index < 0 || index > length) {
        PyErr_SetString(PyExc_ValueError, "DOM token index is out of range");
        return nullptr;
    }
    // Bound a native turn, returning to Python at every semantic boundary and
    // at least every 1024 tokens. Constructors still own all node initialization.
    const Py_ssize_t limit = index + (length - index < 1024 ? length - index : 1024);
    for (; index < limit; ++index) {
        if (index >= PyList_Size(tokens)) break;
        PyObject* current_borrowed = PyList_GetItem(stack, PyList_Size(stack) - 1);
        if (!current_borrowed) return nullptr;
        Ref current(Py_NewRef(current_borrowed));
        Ref current_tag(PyObject_GetAttr(current.get(), state->keys[LocalName]));
        if (!current_tag) return nullptr;
        int ordinary_parent = PyDict_Contains(classes, current_tag.get());
        if (ordinary_parent < 0) return nullptr;
        if (!ordinary_parent && !word(current_tag.get(), "body")) break;
        PyObject* token_borrowed = PyList_GetItem(tokens, index);
        if (!token_borrowed) return nullptr;
        Ref token(Py_NewRef(token_borrowed));
        if (!sequence(token.get(), 2)) return nullptr;
        PyObject* kind = PyTuple_GetItem(token.get(), 0);
        PyObject* value = PyTuple_GetItem(token.get(), 1);
        if (word(kind, "start")) {
            if (!sequence(value, 3)) return nullptr;
            PyObject* tag = PyTuple_GetItem(value, 0);
            PyObject* attrs = PyTuple_GetItem(value, 1);
            PyObject* closed = PyTuple_GetItem(value, 2);
            if (!PyUnicode_Check(tag) || !PyList_Check(attrs)) {
                PyErr_SetString(PyExc_TypeError, "invalid DOM start token");
                return nullptr;
            }
            PyObject* cls = PyDict_GetItemWithError(classes, tag);
            if (!cls) {
                if (PyErr_Occurred()) return nullptr;
                break;
            }
            Ref class_owner(Py_NewRef(cls));
            Ref init(PyObject_GetAttrString(cls, "__init__"));
            Ref hook(PyObject_GetAttrString(cls, "_on_tree_inserted"));
            if (!init || !hook) return nullptr;
            if (init.get() != element_init || hook.get() != Py_None) break;
            Ref attributes(PyDict_New());
            if (!attributes) return nullptr;
            bool handler = false;
            for (Py_ssize_t n = 0; n < PyList_Size(attrs); ++n) {
                PyObject* pair = PyList_GetItem(attrs, n);
                if (!sequence(pair, 2)) return nullptr;
                PyObject* key = PyTuple_GetItem(pair, 0);
                PyObject* item = PyTuple_GetItem(pair, 1);
                if (!PyUnicode_Check(key) || (item != Py_None && !PyUnicode_Check(item))) {
                    PyErr_SetString(PyExc_TypeError, "invalid DOM attribute");
                    return nullptr;
                }
                Py_ssize_t size = PyUnicode_GetLength(key);
                if (size < 0) return nullptr;
                if (size >= 2 && PyUnicode_ReadChar(key, 0) == 'o'
                        && PyUnicode_ReadChar(key, 1) == 'n') { handler = true; break; }
                Ref empty(item == Py_None ? PyUnicode_FromString("") : nullptr);
                if (item == Py_None && !empty) return nullptr;
                if (PyDict_SetItem(attributes.get(), key, item == Py_None ? empty.get() : item) < 0)
                    return nullptr;
            }
            if (handler) break;
            if (!consume(state, tokens, parser, index)) return nullptr;
            // Match the Python tree builder's auto-closing algorithm, including
            // an already-open paragraph at this batch's initial boundary.
            PyObject* close_set = PyDict_GetItemWithError(closers, tag);
            if (!close_set && PyErr_Occurred()) return nullptr;
            Ref close_owner(close_set ? Py_NewRef(close_set) : nullptr);
            while (close_set && PyList_Size(stack) > 1) {
                PyObject* current = PyList_GetItem(stack, PyList_Size(stack) - 1);
                Ref name(PyObject_GetAttrString(current, "localName"));
                if (!name) return nullptr;
                int contains = PySet_Contains(close_set, name.get());
                if (contains < 0) return nullptr;
                if (!contains) break;
                if (PySequence_DelItem(stack, PyList_Size(stack) - 1) < 0) return nullptr;
            }
            PyObject* element_args[] = {tag, services, attributes.get()};
            Ref node(PyObject_Vectorcall(cls, element_args, 1, state->element_keywords));
            if (!node) return nullptr;
            Ref empty(PyUnicode_FromString(""));
            if (!empty || PyObject_SetAttr(node.get(), state->keys[RawText], empty.get()) < 0
                    || PyObject_SetAttr(node.get(), state->keys[ParserCreated], Py_True) < 0
                    || !append(state, stack, node.get(), methods)) return nullptr;
            int is_closed = PyObject_IsTrue(closed);
            int is_void = PySet_Contains(voids, tag);
            if (is_closed < 0 || is_void < 0) return nullptr;
            if (!is_closed && !is_void) {
                Ref parent(PyObject_GetAttr(node.get(), state->keys[Parent]));
                if (!parent || (parent.get() != Py_None && PyList_Append(stack, node.get()) < 0))
                    return nullptr;
            }
        } else if (word(kind, "end")) {
            if (!PyUnicode_Check(value)) {
                PyErr_SetString(PyExc_TypeError, "invalid DOM end token");
                return nullptr;
            }
            int ordinary = PyDict_Contains(classes, value);
            if (ordinary < 0) return nullptr;
            if (!ordinary) break;
            if (!consume(state, tokens, parser, index)) return nullptr;
            for (Py_ssize_t n = PyList_Size(stack) - 1; n > 0; --n) {
                Ref tag(PyObject_GetAttrString(PyList_GetItem(stack, n), "localName"));
                if (!tag) return nullptr;
                int equal = PyObject_RichCompareBool(tag.get(), value, Py_EQ);
                if (equal < 0) return nullptr;
                if (equal) {
                    if (PyList_SetSlice(stack, n, PyList_Size(stack), nullptr) < 0) return nullptr;
                    break;
                }
            }
        } else if (word(kind, "data")) {
            if (!PyUnicode_Check(value)) {
                PyErr_SetString(PyExc_TypeError, "invalid DOM text token");
                return nullptr;
            }
            if (!consume(state, tokens, parser, index)) return nullptr;
            Py_ssize_t amp = PyUnicode_FindChar(value, '&', 0, PyUnicode_GetLength(value), 1);
            if (amp == -2) return nullptr;
            Ref decoded(amp >= 0 ? PyObject_CallFunctionObjArgs(decode, value, nullptr)
                                 : Py_NewRef(value));
            PyObject* text_args[] = {decoded.get(), services};
            Ref node(decoded ? PyObject_Vectorcall(text_type, text_args, 1, state->text_keywords) : nullptr);
            if (!node || !append(state, stack, node.get(), methods)) return nullptr;
        } else {
            break;
        }
    }
    if (PyErr_CheckSignals() < 0) return nullptr;
    return PyLong_FromSsize_t(index);
}

PyMethodDef methods[] = {
    {"initialize_target", reinterpret_cast<PyCFunction>(initialize_target), METH_FASTCALL, "Initialize EventTarget storage."},
    {"initialize_node", reinterpret_cast<PyCFunction>(initialize_node), METH_FASTCALL, "Initialize Node storage and its live child view."},
    {"build_batch", build, METH_VARARGS, "Construct a bounded ordinary HTML token batch."},
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
}

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
