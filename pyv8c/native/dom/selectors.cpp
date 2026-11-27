#include "dom.h"

#include <algorithm>
#include <memory>
#include <new>
#include <string_view>

namespace pyv8c::dom {

namespace {

enum class AttributeOp { present, equal, prefix, suffix, contains, dash };

struct AttributeTest {
    AttributeOp op;
    std::string_view wanted;
    Py_ssize_t column;
};

struct AttributeColumn {
    PyObject* key;
    bool text;
};

class PinnedStrings {
    std::unique_ptr<PyObject*[]> references_;
    Py_ssize_t count_ = 0;
public:
    explicit PinnedStrings(Py_ssize_t capacity) :
        references_(new (std::nothrow) PyObject*[capacity]) {}
    ~PinnedStrings() {
        for (Py_ssize_t i = 0; i < count_; ++i) Py_DECREF(references_[i]);
    }
    explicit operator bool() const { return static_cast<bool>(references_); }
    void keep(PyObject* value) { references_[count_++] = Py_NewRef(value); }
};

bool utf8_view(PyObject* value, std::string_view& result) {
    Py_ssize_t length = 0;
    const char* data = PyUnicode_AsUTF8AndSize(value, &length);
    if (!data) return false;
    result = std::string_view(data, static_cast<size_t>(length));
    return true;
}

PyObject* unsupported_unicode() {
    if (!PyErr_ExceptionMatches(PyExc_UnicodeEncodeError)) return nullptr;
    PyErr_Clear();
    return Py_NewRef(Py_None);
}

bool attribute_matches(std::string_view actual, const AttributeTest& test) noexcept {
    if (!actual.data()) return false;
    const auto wanted = test.wanted;
    switch (test.op) {
        case AttributeOp::present: return true;
        case AttributeOp::equal: return actual == wanted;
        case AttributeOp::prefix:
            return !wanted.empty() && actual.compare(0, wanted.size(), wanted) == 0;
        case AttributeOp::suffix:
            return !wanted.empty() && actual.size() >= wanted.size()
                && actual.compare(actual.size() - wanted.size(), wanted.size(), wanted) == 0;
        case AttributeOp::contains: return !wanted.empty() && actual.find(wanted) != actual.npos;
        case AttributeOp::dash:
            return actual == wanted || (actual.size() > wanted.size()
                && actual[wanted.size()] == '-' && actual.compare(0, wanted.size(), wanted) == 0);
    }
    return false;
}

constexpr Py_ssize_t max_predicates = 16;

int compile_attribute_tests(PyObject* predicates, AttributeTest* tests, AttributeColumn* columns,
                            Py_ssize_t& column_count) {
    const Py_ssize_t width = PyTuple_Size(predicates);
    column_count = 0;
    for (Py_ssize_t i = 0; i < width; ++i) {
        PyObject* rule = PyTuple_GetItem(predicates, i);
        if (!PyTuple_CheckExact(rule) || PyTuple_Size(rule) != 3) return 0;
        PyObject* key = PyTuple_GetItem(rule, 0);
        PyObject* op = PyTuple_GetItem(rule, 1);
        PyObject* wanted = PyTuple_GetItem(rule, 2);
        if (!PyUnicode_CheckExact(key) || !PyUnicode_CheckExact(wanted)) return 0;
        if (op == Py_None) tests[i].op = AttributeOp::present;
        else if (word(op, "=")) tests[i].op = AttributeOp::equal;
        else if (word(op, "^=")) tests[i].op = AttributeOp::prefix;
        else if (word(op, "$=")) tests[i].op = AttributeOp::suffix;
        else if (word(op, "*=")) tests[i].op = AttributeOp::contains;
        else if (word(op, "|=")) tests[i].op = AttributeOp::dash;
        else return 0;
        Py_ssize_t column = 0;
        for (; column < column_count; ++column) {
            const int equal = PyObject_RichCompareBool(columns[column].key, key, Py_EQ);
            if (equal < 0) return -1;
            if (equal) break;
        }
        if (column == column_count) {
            columns[column] = {key, false};
            ++column_count;
        }
        columns[column].text |= tests[i].op != AttributeOp::present;
        tests[i].column = column;
        if (!utf8_view(wanted, tests[i].wanted)) {
            if (!PyErr_ExceptionMatches(PyExc_UnicodeEncodeError)) return -1;
            PyErr_Clear();
            return 0;
        }
    }
    return 1;
}

}  // namespace

PyObject* match_attribute_batch(PyObject* module, PyObject* args) {
    PyObject *nodes, *tag, *predicates, *element_type, *attribute_type;
    PyObject* default_access;
    if (!PyArg_ParseTuple(args, "OOOOOO:match_attribute_batch", &nodes, &tag, &predicates,
                          &element_type, &attribute_type, &default_access))
        return nullptr;
    constexpr Py_ssize_t max_rows = 4096;
    if (!PyTuple_CheckExact(nodes) || !PyTuple_CheckExact(predicates) || !PyUnicode_CheckExact(tag)
        || !PyType_Check(element_type) || !PyType_Check(attribute_type)
        || !PyTuple_CheckExact(default_access) || PyTuple_Size(default_access) != 3)
        return Py_NewRef(Py_None);
    const Py_ssize_t count = PyTuple_Size(nodes), width = PyTuple_Size(predicates);
    if (width < 1 || width > max_predicates) return Py_NewRef(Py_None);
    auto* state = static_cast<State*>(PyModule_GetState(module));

    AttributeTest tests[max_predicates];
    AttributeColumn columns[max_predicates];
    Py_ssize_t column_count;
    const int compiled = compile_attribute_tests(predicates, tests, columns, column_count);
    if (compiled < 0) return nullptr;
    if (!compiled) return Py_NewRef(Py_None);

    Ref result(PyList_New(0));
    if (!result) return nullptr;
    for (Py_ssize_t start = 0; start < count;) {
        const Py_ssize_t rows = std::min(max_rows, count - start), cells = rows * column_count;
        const std::unique_ptr<std::string_view[]> values(new (std::nothrow) std::string_view[cells]);
        const std::unique_ptr<Py_ssize_t[]> matches(new (std::nothrow) Py_ssize_t[rows]);
        PinnedStrings pinned(cells);
        if (!values || !matches || !pinned) return PyErr_NoMemory();
        // Resolve the first concrete type's ordinary property once per chunk.
        // Mixed/custom nodes use the descriptor path from their first occurrence.
        Ref first_type(PyObject_Type(PyTuple_GetItem(nodes, start)));
        if (!first_type) return nullptr;
        auto* type = reinterpret_cast<PyTypeObject*>(first_type.get());
        PyTypeObject* fast_type = nullptr;
        if (Py_IS_TYPE(first_type.get(), &PyType_Type)
            && PyType_IsSubtype(type, reinterpret_cast<PyTypeObject*>(element_type))) {
            constexpr Key access[] = {Attributes, GetAttribute, GetAttr};
            fast_type = type;
            for (Py_ssize_t i = 0; i < 3; ++i) {
                Ref current(PyObject_GetAttr(first_type.get(), state->keys[access[i]]));
                if (!current && !PyErr_ExceptionMatches(PyExc_AttributeError)) return nullptr;
                if (!current) PyErr_Clear();
                if (current.get() != PyTuple_GetItem(default_access, i)) {
                    fast_type = nullptr;
                    break;
                }
            }
        }
        for (Py_ssize_t row = 0; row < rows; ++row) {
            PyObject* node = PyTuple_GetItem(nodes, start + row);
            if (fast_type && !Py_IS_TYPE(node, fast_type)) fast_type = nullptr;
            if (!fast_type) {
                const int element = PyObject_IsInstance(node, element_type);
                if (element < 0) return nullptr;
                if (!element) return Py_NewRef(Py_None);
            }
            Ref local_name(PyObject_GetAttr(node, state->keys[LocalName]));
            if (!local_name) return nullptr;
            if (!PyUnicode_CheckExact(local_name.get())) return Py_NewRef(Py_None);
            const int same_tag = PyObject_RichCompareBool(local_name.get(), tag, Py_EQ);
            if (same_tag < 0) return nullptr;
            if (!same_tag) return Py_NewRef(Py_None);
            PyObject* value_map = PyObject_GetAttr(node, state->keys[fast_type ? AttributeMap : Attributes]);
            if (!value_map && fast_type && PyErr_ExceptionMatches(PyExc_AttributeError)) {
                PyErr_Clear();
                fast_type = nullptr;
                value_map = PyObject_GetAttr(node, state->keys[Attributes]);
            }
            Ref attributes(value_map);
            if (!attributes) return nullptr;
            if (!Py_IS_TYPE(attributes.get(), reinterpret_cast<PyTypeObject*>(attribute_type))
                || !PyDict_Check(attributes.get())) return Py_NewRef(Py_None);

            for (Py_ssize_t column = 0; column < column_count; ++column) {
                const Py_ssize_t offset = row * column_count + column;
                PyObject* value = PyDict_GetItemWithError(attributes.get(), columns[column].key);
                if (!value && PyErr_Occurred()) return nullptr;
                if (!value || value == Py_None) break;
                if (columns[column].text) {
                    if (!PyUnicode_CheckExact(value)) return Py_NewRef(Py_None);
                    if (!utf8_view(value, values[offset])) return unsupported_unicode();
                    pinned.keep(value);
                } else {
                    values[offset] = std::string_view("");
                }
            }
        }

        // The immutable node tuple keeps result identities stable across chunks.
        // Only pinned string buffers and native types enter the unlocked loop.
        Py_ssize_t length = 0;
        Py_BEGIN_ALLOW_THREADS
        for (Py_ssize_t row = 0; row < rows; ++row) {
            const auto* values_for_node = values.get() + row * column_count;
            Py_ssize_t test = 0;
            while (test < width && attribute_matches(values_for_node[tests[test].column], tests[test])) ++test;
            if (test == width) matches[length++] = row;
        }
        Py_END_ALLOW_THREADS

        if (PyErr_CheckSignals() < 0) return nullptr;
        for (Py_ssize_t i = 0; i < length; ++i) {
            if (PyList_Append(result.get(), PyTuple_GetItem(nodes, start + matches[i])) < 0) return nullptr;
        }
        start += rows;
    }
    return Py_NewRef(result.get());
}

namespace {

struct ProjectedCell {
    PyObject* value = nullptr;
    std::string_view view;
    ~ProjectedCell() { Py_XDECREF(value); }
};

struct AttributeProjection {
    Py_ssize_t rows, columns;
    Py_ssize_t readers = 0;  // Accessed only while attached.
    std::unique_ptr<ProjectedCell[]> cells;

    AttributeProjection(Py_ssize_t row_count, Py_ssize_t column_count)
        : rows(row_count), columns(column_count),
          cells(new (std::nothrow) ProjectedCell[row_count * column_count]) {}
};

constexpr const char* projection_name = "pyv8c._native.dom.AttributeProjection";

void destroy_projection(PyObject* capsule) {
    delete static_cast<AttributeProjection*>(PyCapsule_GetPointer(capsule, projection_name));
}

int load_projection_row(PyObject* values, ProjectedCell* cells, Py_ssize_t columns) {
    if (!PyTuple_CheckExact(values) || PyTuple_Size(values) != columns) return 0;
    for (Py_ssize_t i = 0; i < columns; ++i) {
        PyObject* value = PyTuple_GetItem(values, i);
        if (value == Py_None) continue;
        if (!PyUnicode_CheckExact(value)) return 0;
        if (!utf8_view(value, cells[i].view)) {
            if (!PyErr_ExceptionMatches(PyExc_UnicodeEncodeError)) return -1;
            PyErr_Clear();
            return 0;
        }
        cells[i].value = Py_NewRef(value);
    }
    return 1;
}

}  // namespace

PyObject* create_attribute_projection(PyObject*, PyObject* args) {
    PyObject* values;
    Py_ssize_t columns;
    if (!PyArg_ParseTuple(args, "On:create_attribute_projection", &values, &columns)) return nullptr;
    if (!PyTuple_CheckExact(values) || columns < 1 || columns > max_predicates)
        return Py_NewRef(Py_None);
    const Py_ssize_t rows = PyTuple_Size(values);
    if (rows > 16384) return Py_NewRef(Py_None);
    std::unique_ptr<AttributeProjection> data(new (std::nothrow) AttributeProjection(rows, columns));
    if (!data || !data->cells) return PyErr_NoMemory();
    for (Py_ssize_t row = 0; row < rows; ++row) {
        const int loaded = load_projection_row(PyTuple_GetItem(values, row),
                                               data->cells.get() + row * columns, columns);
        if (loaded < 0) return nullptr;
        if (!loaded) return Py_NewRef(Py_None);
    }
    PyObject* capsule = PyCapsule_New(data.get(), projection_name, destroy_projection);
    if (capsule != nullptr) data.release();
    return capsule;
}

PyObject* update_attribute_projection(PyObject*, PyObject* args) {
    PyObject *capsule, *values;
    Py_ssize_t row;
    if (!PyArg_ParseTuple(args, "OnO:update_attribute_projection", &capsule, &row, &values))
        return nullptr;
    auto* data = static_cast<AttributeProjection*>(PyCapsule_GetPointer(capsule, projection_name));
    if (!data) return nullptr;
    if (row < 0 || row >= data->rows || data->readers) return Py_NewRef(Py_False);

    ProjectedCell replacement[max_predicates];
    const int loaded = load_projection_row(values, replacement, data->columns);
    if (loaded < 0) return nullptr;
    if (!loaded) return Py_NewRef(Py_False);
    auto* cells = data->cells.get() + row * data->columns;
    for (Py_ssize_t i = 0; i < data->columns; ++i) {
        std::swap(replacement[i].value, cells[i].value);
        std::swap(replacement[i].view, cells[i].view);
    }
    return Py_NewRef(Py_True);
}

PyObject* match_attribute_projection(PyObject*, PyObject* args) {
    PyObject *capsule, *nodes, *predicates;
    if (!PyArg_ParseTuple(args, "OOO:match_attribute_projection", &capsule, &nodes, &predicates))
        return nullptr;
    auto* data = static_cast<AttributeProjection*>(PyCapsule_GetPointer(capsule, projection_name));
    if (!data) return nullptr;
    if (!PyTuple_CheckExact(nodes) || PyTuple_Size(nodes) != data->rows
        || !PyTuple_CheckExact(predicates)) return Py_NewRef(Py_None);
    const Py_ssize_t width = PyTuple_Size(predicates);
    if (width < 1 || width > max_predicates) return Py_NewRef(Py_None);
    AttributeTest tests[max_predicates];
    AttributeColumn columns[max_predicates];
    Py_ssize_t column_count;
    const int compiled = compile_attribute_tests(predicates, tests, columns, column_count);
    if (compiled < 0) return nullptr;
    if (!compiled || column_count != data->columns) return Py_NewRef(Py_None);
    const std::unique_ptr<Py_ssize_t[]> matches(new (std::nothrow) Py_ssize_t[data->rows]);
    if (!matches) return PyErr_NoMemory();

    // Capsule ownership pins every UTF-8 buffer. Updates are refused while a
    // scan is reading; no Python API or mutable DOM state is accessed unlocked.
    Py_ssize_t length = 0;
    ++data->readers;
    Py_BEGIN_ALLOW_THREADS
    for (Py_ssize_t row = 0; row < data->rows; ++row) {
        const auto* cells = data->cells.get() + row * data->columns;
        Py_ssize_t test = 0;
        while (test < width && attribute_matches(cells[tests[test].column].view, tests[test])) ++test;
        if (test == width) matches[length++] = row;
    }
    Py_END_ALLOW_THREADS
    --data->readers;

    if (PyErr_CheckSignals() < 0) return nullptr;
    Ref result(PyList_New(length));
    if (!result) return nullptr;
    for (Py_ssize_t i = 0; i < length; ++i) {
        if (PyList_SetItem(result.get(), i, Py_NewRef(PyTuple_GetItem(nodes, matches[i]))) < 0)
            return nullptr;
    }
    return Py_NewRef(result.get());
}

}  // namespace pyv8c::dom
