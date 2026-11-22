"""The rules behind a style sheet: reading them, writing them out, and editing them.

A sheet's text is read into rule objects once and they are what a page gets, because
that is what Chrome hands out: a CSSRuleList of live CSSStyleRule and CSSMediaRule
objects that answer their own cssText and that insertRule and deleteRule edit in place.

The serialisation is Blink's rather than the parse's -- a style rule on one line with a
space either side of the braces, a grouping rule over several lines with its children
indented two spaces deeper -- because a page that reads cssText back compares it.
"""
import re

from pyv8c import pyv8

from ...idl import interface
from ...foundation.arguments import MISSING, dom_string, integer, missing_error
from ..page_errors import IndexSizeError, dom_exception
from .cssom import CSSStyleDeclaration
from ..dom.mixins import ReadOnlyIndexedPropertyMixin


#: Blink's wording when the text an edit was given is not a rule at all.
PARSE_MESSAGE = ("Failed to execute 'insertRule' on 'CSSStyleSheet': Failed to parse the"
                 " rule '{}'.")


#: The size refusal CSSOM raises for an index the list does not have.
RANGE_MESSAGE = "Index or size is negative or greater than the allowed amount."


def _prelude_name(prelude):
    """``@media screen and (x)`` as ``("media", "screen and (x)")``."""
    match = re.match(r"@([A-Za-z-]+)\s*(.*)", prelude, re.S)
    return (match.group(1).lower(), match.group(2).strip()) if match else ("", prelude)


def parse_rules(text):
    """*text* as ``(kind, prelude, block)`` triples, nested blocks kept whole.

    The braces are matched rather than searched, so a rule whose body holds rules of
    its own is one entry with its body intact.
    """
    rules, index, length = [], 0, len(text)
    while index < length:
        opening = text.find("{", index)
        if opening < 0:
            break
        depth, closing = 1, opening + 1
        while closing < length and depth:
            if text[closing] == "{":
                depth += 1
            elif text[closing] == "}":
                depth -= 1
            closing += 1
        prelude = text[index:opening].strip()
        body = text[opening + 1:closing - 1 if depth == 0 else length]
        if prelude:
            rules.append(("grouping" if prelude.startswith("@") else "style",
                          prelude, body))
        index = closing
    return rules


def _rule_class(kind, prelude):
    if kind != "grouping":
        return CSSStyleRule
    if _prelude_name(prelude)[0] == "font-face":
        return CSSFontFaceRule
    return CSSMediaRule if _prelude_name(prelude)[0] == "media" else CSSGroupingRule


def _build(kind, prelude, block, sheet, parent):
    """The rule object for one parsed rule.

    A rule is an interface no page may construct, so the object is made directly and
    then given what was parsed.
    """
    cls = _rule_class(kind, prelude)
    rule = cls.__new__(cls)
    rule._init_rule(prelude, block, sheet, parent)
    return rule


def build_rules(text, sheet, parent=None):
    """The rule objects for *text*, in order."""
    return [_build(kind, prelude, block, sheet, parent)
            for kind, prelude, block in parse_rules(text)]


def insert_rule(owner, text, index):
    """Put *text* into *owner* at *index*, the way CSSOM does."""
    parsed = parse_rules(text)
    rules = owner.get_cssRules()._rules
    if not parsed:
        raise dom_exception("SyntaxError", PARSE_MESSAGE.format(text))
    if not 0 <= index <= len(rules):
        raise IndexSizeError(RANGE_MESSAGE)
    kind, prelude, block = parsed[0]
    rules.insert(index, _build(kind, prelude, block, owner._owner_sheet(), owner))
    sheet = owner._owner_sheet()
    if sheet is not None:
        sheet.rules_changed()
    return index


@interface('CSSRule', None)
class CSSRule:
    """What every rule answers: its type, its text, and where it came from."""

    RULE_TYPE = 0

    def _init_rule(self, prelude, block, sheet, parent):
        self._prelude, self._block = prelude, block
        self._sheet, self._parent_rule = sheet, parent

    def get_type(self): return self.RULE_TYPE
    def get_cssText(self): return self._serialise()
    def get_parentRule(self): return self._parent_rule or pyv8.Null
    def get_parentStyleSheet(self): return self._sheet or pyv8.Null
    def _owner_sheet(self):
        """The sheet this rule belongs to, however deep in a grouping rule it sits."""
        rule = self
        while rule is not None:
            if getattr(rule, "_sheet", None) is not None:
                return rule._sheet
            rule = getattr(rule, "_parent_rule", None)
        return None
    def _serialise(self, indent=0):
        return " " * indent + "%s { %s }" % (self._prelude, self._block.strip())


class _DeclarationRule(CSSRule):
    """The live declaration block shared by style and font-face rules."""
    def _init_rule(self, prelude, block, sheet, parent):
        super()._init_rule(prelude, block, sheet, parent)
        self._style = CSSStyleDeclaration(block=block, parent_rule=self)

    def get_style(self): return self._style
    def _serialise(self, indent=0):
        body = " ".join("%s: %s%s;" % (name, value, " !important" if priority else "")
                        for name, value, priority in self._style._declarations())
        return " " * indent + "%s { %s }" % (self._prelude, body)


@interface('CSSStyleRule', 'CSSRule')
class CSSStyleRule(_DeclarationRule):
    """A selector and the declarations it carries."""
    RULE_TYPE = 1

    def get_selectorText(self): return self._prelude


@interface('CSSFontFaceRule', 'CSSRule')
class CSSFontFaceRule(_DeclarationRule):
    """A font-face declaration block, retained when other rules are edited."""
    RULE_TYPE = 5


@interface('CSSGroupingRule', 'CSSRule')
class CSSGroupingRule(CSSRule):
    """A rule that holds rules of its own, such as @media."""

    def _init_rule(self, prelude, block, sheet, parent):
        super()._init_rule(prelude, block, sheet, parent)
        self._rules = None

    def get_cssRules(self):
        if self._rules is None:
            self._rules = build_rules(self._block, self._owner_sheet(), self)
        return CSSRuleList(self._rules)

    def _serialise(self, indent=0):
        children = "\n".join(rule._serialise(indent + 2)
                             for rule in self.get_cssRules()._rules)
        return " " * indent + "%s {\n%s\n%s}" % (self._prelude, children, " " * indent)

    def fn_insertRule(self, rule=MISSING, index=0, *_extra):
        if rule is MISSING:
            raise missing_error('CSSGroupingRule', 'insertRule', 2, 0, constructor=False)
        return insert_rule(self, str(dom_string(rule)), integer(index))

    def fn_deleteRule(self, index=MISSING, *_extra):
        if index is MISSING:
            raise missing_error('CSSGroupingRule', 'deleteRule', 1, 0, constructor=False)
        rules = self.get_cssRules()._rules
        position = integer(index)
        if not 0 <= position < len(rules):
            raise IndexSizeError(RANGE_MESSAGE)
        del rules[position]
        sheet = self._owner_sheet()
        if sheet is not None:
            sheet.rules_changed()
        return None


@interface('CSSConditionRule', 'CSSGroupingRule')
class CSSConditionRule(CSSGroupingRule):
    """A grouping rule whose prelude is a condition, which is what @media is."""


@interface('CSSMediaRule', 'CSSConditionRule')
class CSSMediaRule(CSSConditionRule):
    """A @media rule, which is a condition rule with a medium of its own."""

    RULE_TYPE = 4

    def get_media(self): return MediaList(_prelude_name(self._prelude)[1])


@interface('MediaList', None)
class MediaList:
    """The medium a @media rule names."""

    def __init__(self, text="", **_):
        self._text = str(text)

    def get_mediaText(self): return self._text
    def get_length(self): return len(self._text.split())
    def fn_item(self, index=MISSING, *_extra):
        if index is MISSING:
            raise missing_error('MediaList', 'item', 1, 0, constructor=False)
        words = self._text.split()
        position = integer(index)
        return words[position] if 0 <= position < len(words) else pyv8.Null


@interface('CSSRuleList', None)
class CSSRuleList(ReadOnlyIndexedPropertyMixin):
    """A sheet's rules as a page sees them: the same list every read, and what an
    edit mutates."""

    def __init__(self, rules=None, **_):
        self._rules = rules if rules is not None else []

    def get_length(self): return len(self._rules)
    def _indexed_items(self): return list(self._rules)
    def fn_item(self, index=MISSING, *_extra):
        if index is MISSING:
            raise missing_error('CSSRuleList', 'item', 1, 0, constructor=False)
        position = integer(index)
        return self._rules[position] if 0 <= position < len(self._rules) else pyv8.Null

__all__ = ["CSSGroupingRule", "CSSConditionRule", "CSSMediaRule", "CSSFontFaceRule", "CSSRule",
           "CSSRuleList", "CSSStyleRule", "MediaList", "build_rules", "insert_rule",
           "parse_rules"]
