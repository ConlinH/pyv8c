#include "fragment_lexer.h"

namespace pyv8c::dom {

namespace {

bool html_space(char ch) {
    return ch == ' ' || ch == '\t' || ch == '\n' || ch == '\r' || ch == '\f';
}

bool ascii_letter(char ch) {
    return (ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z');
}

bool name_byte(char ch) {
    return ascii_letter(ch) || (ch >= '0' && ch <= '9')
        || ch == '-' || ch == '_' || ch == ':' || ch == '.';
}

void skip_html_space(std::string_view source, std::size_t& cursor) {
    while (cursor < source.size() && html_space(source[cursor])) ++cursor;
}

std::string_view fragment_name(std::string_view source, std::size_t& cursor) {
    const std::size_t start = cursor;
    while (cursor < source.size() && name_byte(source[cursor])) ++cursor;
    const std::size_t length = cursor - start;
    return length <= max_fragment_name ? source.substr(start, length) : std::string_view{};
}

bool plain_fragment_text(std::string_view text) {
    // Keep an entire text run together on fallback, so entities retain the
    // original number and boundaries of Text nodes.
    return text.find('&') == text.npos && text.find('\0') == text.npos;
}

bool fragment_tag(std::string_view source, std::size_t& cursor, FragmentToken& token, FragmentBatch& batch) {
    const std::size_t length = source.size();
    ++cursor;  // '<'
    const bool closing = cursor < length && source[cursor] == '/';
    if (closing) ++cursor;
    if (cursor == length || !ascii_letter(source[cursor])) return false;
    token.kind = closing ? FragmentKind::end : FragmentKind::start;
    token.value = fragment_name(source, cursor);
    if (token.value.empty()) return false;
    if (closing) {
        skip_html_space(source, cursor);
        if (cursor == length || source[cursor] != '>') return false;
        ++cursor;
        return true;
    }

    token.first_attribute = batch.attributes_used;
    while (cursor < length) {
        if (source[cursor] == '>') {
            ++cursor;
            return true;
        }
        if (source[cursor] == '/' && cursor + 1 < length && source[cursor + 1] == '>') {
            cursor += 2;
            token.closed = true;
            return true;
        }
        if (!html_space(source[cursor])) return false;
        skip_html_space(source, cursor);
        if (cursor == length) return false;
        if (source[cursor] == '>' || source[cursor] == '/') continue;
        auto name = fragment_name(source, cursor);
        if (name.empty() || batch.attributes_used == max_fragment_attributes) return false;
        const std::size_t name_end = cursor;
        skip_html_space(source, cursor);
        std::string_view value;
        if (cursor < length && source[cursor] == '=') {
            ++cursor;
            skip_html_space(source, cursor);
            if (cursor == length) return false;
            const char quote = source[cursor];
            if (quote == '\'' || quote == '"') {
                const std::size_t end = source.find(quote, ++cursor);
                if (end == source.npos) return false;
                value = source.substr(cursor, end - cursor);
                if (!plain_fragment_text(value)) return false;
                cursor = end + 1;
            } else {
                // Leave tolerant punctuation and slash handling to HTMLParser.
                value = fragment_name(source, cursor);
                if (value.empty() || (cursor < length && source[cursor] == '/')) return false;
            }
        } else {
            cursor = name_end;
        }
        batch.attributes[batch.attributes_used++] = {name, value};
        ++token.attribute_count;
    }
    return false;
}

}  // namespace

std::size_t scan_fragment(std::string_view source, std::size_t cursor, FragmentBatch& batch) {
    while (cursor < source.size() && batch.size < construction_batch) {
        FragmentToken token{};
        token.offset = cursor;
        if (source[cursor] == '<') {
            if (!fragment_tag(source, cursor, token, batch)) {
                batch.fallback = true;
                return token.offset;
            }
        } else {
            std::size_t end = source.find('<', cursor);
            if (end == source.npos) end = source.size();
            token.kind = FragmentKind::data;
            token.value = source.substr(cursor, end - cursor);
            if (!plain_fragment_text(token.value)) {
                batch.fallback = true;
                return token.offset;
            }
            cursor = end;
        }
        batch.tokens[batch.size++] = token;
    }
    return cursor;
}

}  // namespace pyv8c::dom
