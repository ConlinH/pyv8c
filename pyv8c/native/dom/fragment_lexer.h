#pragma once

#include <cstddef>
#include <string_view>

namespace pyv8c::dom {

constexpr std::size_t construction_batch = 1024;

// The fragment lexer recognizes only syntax with unambiguous HTMLParser events.
// All its views borrow the immutable UTF-8 buffer pinned by the call arguments.
constexpr std::size_t max_fragment_name = 127;
constexpr std::size_t max_fragment_attributes = 4096;

struct FragmentAttribute {
    std::string_view name, value;  // Null view: a boolean attribute.
};

enum class FragmentKind { start, end, data };

struct FragmentToken {
    FragmentKind kind;
    std::string_view value;
    std::size_t offset, first_attribute, attribute_count;
    bool closed;
};

struct FragmentBatch {
    FragmentToken tokens[construction_batch];
    FragmentAttribute attributes[max_fragment_attributes];
    std::size_t size = 0, attributes_used = 0;
    bool fallback = false;
};

std::size_t scan_fragment(std::string_view source, std::size_t cursor, FragmentBatch& batch);

}  // namespace pyv8c::dom
