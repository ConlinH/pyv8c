#include "dom.h"
#include "fragment_lexer.h"

#include <algorithm>
#include <functional>
#include <memory>
#include <new>

namespace pyv8c::dom {

namespace {

bool sequence(PyObject* value, Py_ssize_t size) {
    if (PyTuple_Check(value) && PyTuple_Size(value) == size) return true;
    PyErr_SetString(PyExc_TypeError, "invalid DOM construction token");
    return false;
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
    const bool empty = (PyList_Check(subtree.get()) && PyList_Size(subtree.get()) == 0)
        || (PyTuple_CheckExact(subtree.get()) && PyTuple_Size(subtree.get()) == 0);
    if (old_parent.get() != Py_None || !empty || hook.get() != Py_None
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

struct TreeBuilder {
    State* state;
    PyObject *stack, *services, *classes, *voids, *closers, *text_type, *element_init, *methods;
    bool fragment;

    bool valid() const {
        if (!sequence(methods, 3)) return false;
        if (PyList_Check(stack) && PyList_Size(stack) > 0 && PyDict_Check(classes) && PyDict_Check(closers))
            return true;
        PyErr_SetString(PyExc_TypeError, "invalid DOM construction state");
        return false;
    }

    int ordinary_parent() const {
        const Py_ssize_t depth = PyList_Size(stack);
        // A checked fragment root may be a ShadowRoot without a localName.
        if (fragment && depth == 1) return 1;
        PyObject* borrowed = PyList_GetItem(stack, depth - 1);
        Ref parent(borrowed ? Py_NewRef(borrowed) : nullptr);
        Ref tag(parent ? PyObject_GetAttr(parent.get(), state->keys[LocalName]) : nullptr);
        if (!tag) return -1;
        const int ordinary = PyDict_Contains(classes, tag.get());
        return ordinary != 0 ? ordinary : word(tag.get(), "body");
    }

    PyObject* element_class(PyObject* tag) const {
        PyObject* cls = PyDict_GetItemWithError(classes, tag);
        if (!cls) return nullptr;
        Ref owner(Py_NewRef(cls));
        Ref init(PyObject_GetAttrString(cls, "__init__"));
        Ref hook(PyObject_GetAttrString(cls, "_on_tree_inserted"));
        if (!init || !hook || init.get() != element_init || hook.get() != Py_None) return nullptr;
        return Py_NewRef(owner.get());
    }

    bool open(PyObject* cls, PyObject* tag, PyObject* attributes, PyObject* closed) const {
        // Use the same auto-closing rule for either source of tokens.
        PyObject* close_set = PyDict_GetItemWithError(closers, tag);
        if (!close_set && PyErr_Occurred()) return false;
        Ref close_owner(close_set ? Py_NewRef(close_set) : nullptr);
        while (close_set && PyList_Size(stack) > 1) {
            PyObject* current = PyList_GetItem(stack, PyList_Size(stack) - 1);
            Ref name(PyObject_GetAttr(current, state->keys[LocalName]));
            if (!name) return false;
            const int contains = PySet_Contains(close_set, name.get());
            if (contains < 0) return false;
            if (!contains) break;
            if (PySequence_DelItem(stack, PyList_Size(stack) - 1) < 0) return false;
        }
        PyObject* args[] = {tag, services, attributes};
        Ref node(PyObject_Vectorcall(cls, args, 1, state->element_keywords));
        if (!node || PyObject_SetAttr(node.get(), state->keys[RawText], state->keys[Empty]) < 0
                || PyObject_SetAttr(node.get(), state->keys[ParserCreated], Py_True) < 0
                || !append(state, stack, node.get(), methods)) return false;
        const int is_closed = PyObject_IsTrue(closed);
        const int is_void = PySet_Contains(voids, tag);
        if (is_closed < 0 || is_void < 0) return false;
        if (is_closed || is_void) return true;
        if (fragment) return PyList_Append(stack, node.get()) == 0;
        Ref parent(PyObject_GetAttr(node.get(), state->keys[Parent]));
        return parent && (parent.get() == Py_None || PyList_Append(stack, node.get()) == 0);
    }

    bool close(PyObject* name) const {
        for (Py_ssize_t n = PyList_Size(stack) - 1; n > 0; --n) {
            Ref tag(PyObject_GetAttr(PyList_GetItem(stack, n), state->keys[LocalName]));
            if (!tag) return false;
            const int equal = PyObject_RichCompareBool(tag.get(), name, Py_EQ);
            if (equal < 0) return false;
            if (equal) return PyList_SetSlice(stack, n, PyList_Size(stack), nullptr) == 0;
        }
        return true;
    }

    bool text(PyObject* value) const {
        PyObject* args[] = {value, services};
        Ref node(PyObject_Vectorcall(text_type, args, 1, state->text_keywords));
        return node && append(state, stack, node.get(), methods);
    }
};

bool consume(State* state, PyObject* tokens, PyObject* parser, Py_ssize_t index) {
    Ref next(PyLong_FromSsize_t(index + 1));
    return next && PyList_SetItem(tokens, index, Py_NewRef(Py_None)) == 0
        && PyObject_SetAttr(parser, state->keys[Index], next.get()) == 0;
}

}  // namespace

PyObject* build_batch(PyObject* module, PyObject* args) {
    auto* state = static_cast<State*>(PyModule_GetState(module));
    PyObject *tokens, *stack, *services, *classes, *voids, *closers, *text_type,
             *decode, *element_init, *methods, *parser;
    Py_ssize_t index;
    int fragment = 0;
    if (!PyArg_ParseTuple(args, "OnOOOOOOOOOO|p", &tokens, &index, &stack, &services,
                          &classes, &voids, &closers, &text_type, &decode, &element_init,
                          &methods, &parser, &fragment)) return nullptr;
    const TreeBuilder tree{state, stack, services, classes, voids, closers, text_type,
                           element_init, methods, fragment != 0};
    if (!PyList_Check(tokens)) {
        PyErr_SetString(PyExc_TypeError, "invalid DOM construction state");
        return nullptr;
    }
    if (!tree.valid()) return nullptr;
    const Py_ssize_t length = PyList_Size(tokens);
    if (index < 0 || index > length) {
        PyErr_SetString(PyExc_ValueError, "DOM token index is out of range");
        return nullptr;
    }
    // Bound a native turn, returning to Python at every semantic boundary and
    // at least every 1024 tokens. Constructors still own all node initialization.
    const Py_ssize_t limit = index + std::min(length - index, static_cast<Py_ssize_t>(construction_batch));
    for (; index < limit; ++index) {
        if (index >= PyList_Size(tokens)) break;
        const int ordinary = tree.ordinary_parent();
        if (ordinary < 0) return nullptr;
        if (!ordinary) break;
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
            Ref cls(tree.element_class(tag));
            if (!cls) {
                if (PyErr_Occurred()) return nullptr;
                break;
            }
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
                if (PyDict_SetItem(attributes.get(), key, item == Py_None ? state->keys[Empty] : item) < 0)
                    return nullptr;
            }
            if (handler) break;
            if (!consume(state, tokens, parser, index)
                    || !tree.open(cls.get(), tag, attributes.get(), closed)) return nullptr;
        } else if (word(kind, "end")) {
            if (!PyUnicode_Check(value)) {
                PyErr_SetString(PyExc_TypeError, "invalid DOM end token");
                return nullptr;
            }
            int ordinary = PyDict_Contains(classes, value);
            if (ordinary < 0) return nullptr;
            if (!ordinary) break;
            if (!consume(state, tokens, parser, index) || !tree.close(value)) return nullptr;
        } else if (word(kind, "data")) {
            if (!PyUnicode_Check(value)) {
                PyErr_SetString(PyExc_TypeError, "invalid DOM text token");
                return nullptr;
            }
            if (!consume(state, tokens, parser, index)) return nullptr;
            // HTMLParser callbacks already separate and decode fragment entities.
            Py_ssize_t amp = fragment ? -1 : PyUnicode_FindChar(value, '&', 0, PyUnicode_GetLength(value), 1);
            if (amp == -2) return nullptr;
            Ref decoded(amp >= 0 ? PyObject_CallFunctionObjArgs(decode, value, nullptr)
                                 : Py_NewRef(value));
            if (!decoded || !tree.text(decoded.get())) return nullptr;
        } else {
            break;
        }
    }
    if (PyErr_CheckSignals() < 0) return nullptr;
    return PyLong_FromSsize_t(index);
}

namespace {

PyObject* fragment_string(std::string_view value) {
    return PyUnicode_FromStringAndSize(value.data(), static_cast<Py_ssize_t>(value.size()));
}

char ascii_lower(char ch) {
    return ch >= 'A' && ch <= 'Z' ? ch + ('a' - 'A') : ch;
}

PyObject* fragment_lower_name(std::string_view name) {
    char folded[max_fragment_name];
    for (size_t i = 0; i < name.size(); ++i) folded[i] = ascii_lower(name[i]);
    return fragment_string({folded, name.size()});
}

// Names repeat across the batch. Share immutable strings while the source
// buffer is pinned, with bounded storage even for many distinct attributes.
class FragmentNames {
    struct Entry {
        std::string_view source;
        PyObject* name = nullptr;
    };
    static constexpr size_t capacity = 64;
    Entry entries_[capacity];

public:
    ~FragmentNames() {
        for (auto& entry : entries_) Py_XDECREF(entry.name);
    }

    PyObject* get(std::string_view source) {
        auto& entry = entries_[std::hash<std::string_view>{}(source) % capacity];
        if (entry.name && entry.source == source) return Py_NewRef(entry.name);
        PyObject* name = fragment_lower_name(source);
        if (!name) return nullptr;
        Py_XDECREF(entry.name);
        entry.name = Py_NewRef(name);
        entry.source = source;
        return name;
    }
};

// Return 1 for a committed token, 0 at a semantic boundary, and -1 on error.
int apply_fragment_token(const TreeBuilder& tree, const FragmentBatch& batch,
                         const FragmentToken& token, FragmentNames& names) {
    const int ordinary = tree.ordinary_parent();
    if (ordinary <= 0) return ordinary;
    if (token.kind == FragmentKind::data) {
        Ref text(fragment_string(token.value));
        return text && tree.text(text.get()) ? 1 : -1;
    }
    Ref tag(names.get(token.value));
    if (!tag) return -1;
    if (token.kind == FragmentKind::end) {
        const int allowed = PyDict_Contains(tree.classes, tag.get());
        if (allowed <= 0) return allowed;
        return tree.close(tag.get()) ? 1 : -1;
    }
    Ref cls(tree.element_class(tag.get()));
    if (!cls) return PyErr_Occurred() ? -1 : 0;
    Ref attributes(PyDict_New());
    if (!attributes) return -1;
    for (size_t i = 0; i < token.attribute_count; ++i) {
        const auto& attribute = batch.attributes[token.first_attribute + i];
        if (attribute.name.size() >= 2 && ascii_lower(attribute.name[0]) == 'o'
                && ascii_lower(attribute.name[1]) == 'n') return 0;
        Ref name(names.get(attribute.name));
        Ref value(attribute.value.data() ? fragment_string(attribute.value) : Py_NewRef(tree.state->keys[Empty]));
        if (!name || !value || PyDict_SetItem(attributes.get(), name.get(), value.get()) < 0) return -1;
    }
    return tree.open(cls.get(), tag.get(), attributes.get(), token.closed ? Py_True : Py_False) ? 1 : -1;
}

}  // namespace

PyObject* build_fragment(PyObject* module, PyObject* args) {
    PyObject *html, *stack, *services, *classes, *voids, *closers, *text_type, *element_init, *methods;
    Py_ssize_t offset;
    if (!PyArg_ParseTuple(args, "UnOOOOOOOO:build_fragment", &html, &offset, &stack, &services,
                          &classes, &voids, &closers, &text_type, &element_init, &methods)) return nullptr;
    const TreeBuilder tree{static_cast<State*>(PyModule_GetState(module)), stack, services, classes,
                           voids, closers, text_type, element_init, methods, true};
    if (!tree.valid()) return nullptr;
    Py_ssize_t length;
    const char* bytes = PyUnicode_AsUTF8AndSize(html, &length);
    if (!bytes) {
        if (!PyErr_ExceptionMatches(PyExc_UnicodeEncodeError)) return nullptr;
        PyErr_Clear();
        Py_RETURN_NONE;
    }
    if (offset < 0 || offset > length || (offset < length && (bytes[offset] & 0xc0) == 0x80)) {
        PyErr_SetString(PyExc_ValueError, "fragment byte offset is out of range");
        return nullptr;
    }
    std::unique_ptr<FragmentBatch> batch(new (std::nothrow) FragmentBatch);
    if (!batch) return PyErr_NoMemory();
    size_t end;
    // The Unicode argument pins the buffer. Only the scanner and its native
    // buffers are used while detached. Object creation and insertion hold the GIL.
    Py_BEGIN_ALLOW_THREADS
    end = scan_fragment({bytes, static_cast<size_t>(length)}, static_cast<size_t>(offset), *batch);
    Py_END_ALLOW_THREADS
    if (PyErr_CheckSignals() < 0) return nullptr;

    FragmentNames names;
    for (size_t i = 0; i < batch->size; ++i) {
        const FragmentToken& token = batch->tokens[i];
        const int applied = apply_fragment_token(tree, *batch, token, names);
        if (applied < 0) return nullptr;
        if (!applied) {
            if (PyErr_CheckSignals() < 0) return nullptr;
            return Py_BuildValue("(nOO)", static_cast<Py_ssize_t>(token.offset), Py_True, Py_True);
        }
    }
    if (PyErr_CheckSignals() < 0) return nullptr;
    const auto next = !batch->fallback && end == static_cast<size_t>(length) ? -1 : static_cast<Py_ssize_t>(end);
    return Py_BuildValue("(nOO)", next, batch->fallback ? Py_True : Py_False, Py_False);
}

}  // namespace pyv8c::dom
