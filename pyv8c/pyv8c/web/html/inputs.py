"""``HTMLInputElement`` plus the constraint-validation types it exposes: input
types, validation patterns and step/selection arithmetic for the tag."""

import re

from pyv8c import pyv8

from ...idl import interface
from ...foundation.arguments import (
    MISSING,
    boolean,
    dom_string,
    integer,
    interface_value,
    is_nullish,
    missing_error,
)
from ..page_errors import IndexSizeError, InvalidStateError, NotSupportedError, PageTypeError
from ..dom.collections import HTMLCollection, HTMLOptionsCollection, NodeList
from ..dom.element import Element
from .elements import HTMLElement
from .utils import form_owner, set_url_value, url_value


def _signed_long(value):
    """An IDL ``long`` argument: the value wraps into 32 signed bits."""
    number = int(float(value)) & 0xFFFFFFFF
    return number - 0x100000000 if number >= 0x80000000 else number


@interface('HTMLTextAreaElement', 'HTMLElement')
class HTMLTextAreaElement(HTMLElement):
    """A textarea with independent default and live values."""

    def get_name(self): return self._attr("name")
    def set_name(self, value): self._set_string("name", value)
    def get_form(self): return self._find_form()
    def _find_form(self):
        return form_owner(self) or pyv8.Null
    def get_defaultValue(self): return str(self.get_textContent())
    def set_defaultValue(self, value): self.set_textContent(str(dom_string(value)))
    def get_value(self):
        return self._raw_value if getattr(self, '_dirty_value', False) else self.get_defaultValue()
    def set_value(self, value):
        self._raw_value = str(dom_string(value)).replace('\r\n', '\n').replace('\r', '\n')
        self._dirty_value = True
    def get_maxLength(self):
        """``-1`` for an attribute that is missing, unparseable or negative."""
        try:
            number = int(str(self.attributes.get("maxlength")))
        except (TypeError, ValueError):
            return -1
        return number if number >= 0 else -1
    def set_maxLength(self, value):
        number = _signed_long(value)
        if number < 0:
            raise IndexSizeError(
                f"Failed to set the 'maxLength' property on '{type(self).__name__}':"
                f" The provided value ({number}) is not positive or 0.")
        self.attributes["maxlength"] = str(number)
    def get_dirName(self): return self._attr("dirname")
    def set_dirName(self, value): self._set_string("dirname", value)


@interface('HTMLOptionElement', 'HTMLElement')
class HTMLOptionElement(HTMLElement):
    """``<option>``: ``selected`` starts from the attribute and can be overridden.
    Chromium keeps selectedness separate from the ``selected`` attribute, so a
    script's set is not undone; ``value`` falls back to the option's text.
    """

    def _select_ancestor(self):
        node = self.parentNode
        while node is not None:
            if isinstance(node, HTMLSelectElement):
                return node
            node = node.parentNode
        return None
    def _explicit_state(self):
        """The selectedness a script or the attribute gave this option, if any."""
        state = getattr(self, "_selected_state", None)
        if state is not None:
            return bool(state)
        return True if "selected" in self.attributes else None
    def get_selected(self):
        """The option's selectedness: explicit state, then the attribute, else a
        single-select's first option (the reset parsing runs, without storing it)."""
        explicit = self._explicit_state()
        if explicit is not None:
            select = self._select_ancestor()
            if explicit and select is not None and 'multiple' not in select.attributes:
                options = select._options()
                return not any(other._explicit_state() is True for other in options[options.index(self) + 1:])
            return explicit
        select = self._select_ancestor()
        return select._selects_first_option(self) if select is not None else False
    def set_selected(self, value):
        self._selected_state = boolean(value)
        select = self._select_ancestor()
        if self._selected_state and select is not None and 'multiple' not in select.attributes:
            for option in select._options():
                if option is not self:
                    option._selected_state = False
    def get_defaultSelected(self): return "selected" in self.attributes
    def set_defaultSelected(self, value):
        if boolean(value): self.attributes["selected"] = ""
        else: self.attributes.pop("selected", None)
    def _own_text(self):
        """The option's own text: its text content with whitespace collapsed."""
        return " ".join(str(self.get_textContent() or "").split())
    def get_text(self): return self._own_text()
    def get_label(self):
        """The label attribute, or the option's text when it carries none."""
        value = self.attributes.get("label")
        return str(value) if value is not None else self._own_text()
    def set_label(self, value):
        self.attributes['label'] = str(dom_string(value))
    def get_value(self):
        value = self.attributes.get("value")
        return str(value) if value is not None else self._own_text()
    def set_value(self, value):
        self.attributes['value'] = str(dom_string(value))


@interface('HTMLSelectElement', 'HTMLElement')
class HTMLSelectElement(HTMLElement):
    """``<select>``: the option list and what is selected.  ``options`` is the live
    view, ``selectedIndex`` reports and moves the selection, ``value`` shortcuts it."""

    def _options(self):
        return [node for node in self._descendants() if isinstance(node, HTMLOptionElement)]
    def get_name(self): return self._attr("name")
    def set_name(self, value): self._set_string("name", value)
    def get_form(self):
        return form_owner(self) or pyv8.Null
    def get_options(self):
        value = getattr(self, "_options_collection", None)
        if value is None:
            value = self._options_collection = HTMLOptionsCollection(owner=self, getter=self._options)
        return value
    def get_selectedIndex(self):
        """The first selected option's position, or -1 when nothing is selected."""
        return next((index for index, option in enumerate(self._options()) if option.get_selected()), -1)
    def set_selectedIndex(self, index):
        """Select one option by position; a position outside the list clears them all."""
        index = integer(index)
        options = self._options()
        chosen = options[index] if 0 <= index < len(options) else None
        for option in options:
            option.set_selected(option is chosen)
    def get_length(self):
        return len(self._options())
    def get_selectedOptions(self):
        value = getattr(self, "_selected_options", None)
        if value is None:
            value = self._selected_options = HTMLCollection(
                owner=self,
                getter=lambda: [option for option in self._options() if option.get_selected()],
            )
        return value
    def _selects_first_option(self, option):
        """Whether ``option`` is this select's default selection: the first option of
        a single-select with no explicit state anywhere, which ``value='missing'`` clears."""
        if "multiple" in self.attributes or str(self.attributes.get("size", "1")) not in ("", "1"):
            return False
        options = self._options()
        if not options or options[0] is not option:
            return False
        return not any(other._explicit_state() is not None for other in options[1:])
    def get_value(self):
        for option in self._options():
            if option.get_selected():
                return option.get_value()
        return ""
    def set_value(self, value):
        value = str(dom_string(value))
        match = next((option for option in self._options() if option.get_value() == value), None)
        for option in self._options():
            option.set_selected(option is match)


#: Input types, in the spelling Chromium's ``HTMLInputElement.type`` accepts.
_INPUT_TYPES = frozenset((
    "hidden", "text", "search", "tel", "url", "email", "password", "date", "month",
    "week", "time", "datetime-local", "number", "range", "color", "checkbox", "radio",
    "file", "submit", "image", "reset", "button",
))
#: Types whose click runs an activation behavior (toggle, submit or reset).
_ACTIVATION_INPUT_TYPES = frozenset(("checkbox", "radio", "submit", "image", "reset"))
#: Types whose ``value`` the user edits as text, so the selection APIs apply.
_SELECTABLE_INPUT_TYPES = frozenset(("text", "search", "tel", "url", "password"))
_DATE_INPUT_TYPES = frozenset(("date", "month", "week", "time", "datetime-local"))
#: Types that participate in constraint validation and step arithmetic.
_STEPPABLE_INPUT_TYPES = frozenset(("number", "range")) | _DATE_INPUT_TYPES
#: Types barred from constraint validation.
_BARRED_INPUT_TYPES = frozenset(("hidden", "reset", "button"))
#: Types that support ``readonly`` and are barred from validation while set.
_READONLY_INPUT_TYPES = frozenset((
    "text", "search", "tel", "url", "email", "password", "date", "month", "week",
    "time", "datetime-local", "number", "color",
))
_DATE_PATTERNS = {
    "date": (r"^(\d{4})-(\d{2})-(\d{2})$", (0, 1, 1)),
    "month": (r"^(\d{4})-(\d{2})$", (0, 1, 0)),
    "week": (r"^(\d{4})-W(\d{2})$", None),
    "time": (r"^(\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?$", None),
    "datetime-local": (r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?$", None),
}
_COLOR_PATTERN = re.compile(r"^#[0-9a-fA-F]{6}$")
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_URL_PATTERN = re.compile(r"^[^\s:]+:[^\s]*$")



@interface('ValidityState', None)
class ValidityState:
    """Constraint-validation flags of one element that owns them."""

    _FLAGS = ("valueMissing", "typeMismatch", "badInput", "patternMismatch", "rangeOverflow",
              "rangeUnderflow", "stepMismatch", "tooLong", "tooShort", "customError")

    def __init__(self, owner=None, **_):
        self._owner = owner
    def _flags(self):
        return self._owner._validity_flags() if self._owner is not None else {}
    def get_valueMissing(self): return self._flags().get("valueMissing", False)
    def get_typeMismatch(self): return self._flags().get("typeMismatch", False)
    def get_badInput(self): return self._flags().get("badInput", False)
    def get_patternMismatch(self): return self._flags().get("patternMismatch", False)
    def get_rangeOverflow(self): return self._flags().get("rangeOverflow", False)
    def get_rangeUnderflow(self): return self._flags().get("rangeUnderflow", False)
    def get_stepMismatch(self): return self._flags().get("stepMismatch", False)
    def get_tooLong(self): return self._flags().get("tooLong", False)
    def get_tooShort(self): return self._flags().get("tooShort", False)
    def get_customError(self): return self._flags().get("customError", False)
    def get_valid(self): return not any(self._flags().values())


@interface('HTMLInputElement', 'HTMLElement')
class HTMLInputElement(HTMLElement):
    """The HTML ``input`` element with Chrome's value, selection and validation
    semantics; ``value``/``checked`` keep dirty flags and validity flags are per type."""

    __idl_overrides__ = {
        "interface": {
            "length": 0
        }
    }

    def __init__(self, tag_name="input", services=None, **kw):
        super().__init__(tag_name, services=services, **kw)
        self._dirty_value = False
        self._raw_value = ""
        self._dirty_checked = False
        self._checked = "checked" in self.attributes
        self._indeterminate = False
        self._custom_message = ""
        self._selection_start = 0
        self._selection_end = 0
        self._selection_direction = "none"
        self._validity_state = None
        self._files = None

    def _long(self, name):
        try: return int(str(self.attributes.get(name)))
        except (TypeError, ValueError): return None

    def get_capture(self):
        """The slot as it was assigned, and null until it is: this attribute is not a
        reflection, so an assignment neither writes nor reads the content attribute."""
        return getattr(self, "_capture", pyv8.Null)
    def set_capture(self, value):
        self._capture = value
    def get_type(self):
        value = self._attr("type").lower()
        return value if value in _INPUT_TYPES else "text"
    def set_type(self, value):
        self._set_string("type", value)

    def get_value(self):
        if self._dirty_value: return self._raw_value
        if self.get_type() in ("checkbox", "radio") and "value" not in self.attributes: return "on"
        if self.get_type() == "color": return self._sanitize_value(self._attr("value"))
        return self._attr("value")
    def set_value(self, value):
        value = dom_string(value)
        self._dirty_value = True
        self._raw_value = self._sanitize_value(str(value))
        self._invalidate_layout()
    def get_defaultValue(self): return self._attr("value")
    def set_defaultValue(self, value):
        value = dom_string(value)
        self.attributes["value"] = str(value)
        if not self._dirty_value: self._raw_value = str(value)

    def _sanitize_value(self, value):
        """The value sanitization algorithm for the element's current type."""
        kind = self.get_type()
        if kind in _STEPPABLE_INPUT_TYPES:
            return value if self._parse_value(kind, value) is not None else ""
        if kind == "email":
            if "multiple" in self.attributes:
                return ",".join(part.strip() for part in value.split(","))
            return value.strip()
        if kind == "color":
            return value.lower() if _COLOR_PATTERN.match(value) else "#000000"
        return value.replace("\r", "").replace("\n", "")

    def get_valueAsNumber(self):
        kind = self.get_type()
        parsed = self._parse_value(kind, self.get_value())
        if kind in _DATE_INPUT_TYPES:
            return float("nan") if parsed is None else float(parsed)
        if kind in ("number", "range"):
            try: return float(self.get_value())
            except ValueError: return float("nan")
        return float("nan")
    def set_valueAsNumber(self, value):
        if is_nullish(value) or isinstance(value, bool) or value != value:
            self.set_value("")
            return
        kind = self.get_type()
        if kind in _DATE_INPUT_TYPES: self.set_value(self._format_value(kind, float(value)))
        else: self.set_value(self._format_number(float(value)))

    def get_valueAsDate(self):
        kind = self.get_type()
        if kind not in _DATE_INPUT_TYPES: return pyv8.Null
        parsed = self._parse_value(kind, self.get_value())
        if parsed is None: return pyv8.Null
        realm = getattr(getattr(self._services, "realm_services", None), "evaluate_script", None)
        if realm is None: return pyv8.Null
        try: return realm(f"new Date({int(parsed)})")
        except Exception:  # a missing realm must not break the getter
            return pyv8.Null
    def set_valueAsDate(self, value):
        if is_nullish(value):
            self.set_value("")
            return
        try: milliseconds = float(value.getTime.call(value))
        except Exception:  # non-Date values are ignored, like Chrome
            return
        self.set_value(self._format_value(self.get_type(), milliseconds))

    def get_checked(self): return self._checked
    def _on_tree_inserted(self):
        if self._checked and self.get_type() == 'radio':
            dirty = self._dirty_checked
            self.set_checked(True)
            self._dirty_checked = dirty

    def _attribute_changed_here(self, name, old, new):
        super()._attribute_changed_here(name, old, new)
        if name == 'checked' and not self._dirty_checked:
            self._checked = new is not None
        if name in ('checked', 'name', 'type', 'form') and self._checked:
            self._on_tree_inserted()

    def set_checked(self, value):
        self._dirty_checked = True
        self._checked = boolean(value)
        if self._checked and self.get_type() == 'radio':
            name = self._attr('name')
            if name:
                owner = self._form_owner()
                for other in self.root_node()._descendants():
                    if (other is not self and isinstance(other, HTMLInputElement)
                            and other.get_type() == 'radio' and other._attr('name') == name
                            and other._form_owner() is owner):
                        other._checked = False
    def get_defaultChecked(self): return "checked" in self.attributes
    def set_defaultChecked(self, value):
        value = boolean(value)
        if value: self.attributes["checked"] = ""
        else: self.attributes.pop("checked", None)
        if not self._dirty_checked: self._checked = value
    def get_indeterminate(self): return self._indeterminate
    def set_indeterminate(self, value): self._indeterminate = boolean(value)

    def get_accept(self): return self._attr("accept")
    def set_accept(self, value): self._set_string("accept", value)
    def get_alt(self): return self._attr("alt")
    def set_alt(self, value): self._set_string("alt", value)
    def get_autocomplete(self): return self._attr("autocomplete")
    def set_autocomplete(self, value): self._set_string("autocomplete", value)
    def get_dirName(self): return self._attr("dirname")
    def set_dirName(self, value): self._set_string("dirname", value)
    def get_disabled(self): return self._bool("disabled")
    def set_disabled(self, value): self._set_bool("disabled", value)
    def get_formAction(self): return self._attr("formaction")
    def set_formAction(self, value): self._set_string("formaction", value)
    def get_formEnctype(self): return self._attr("formenctype")
    def set_formEnctype(self, value): self._set_string("formenctype", value)
    def get_formMethod(self): return self._attr("formmethod")
    def set_formMethod(self, value): self._set_string("formmethod", value)
    def get_formNoValidate(self): return self._bool("formnovalidate")
    def set_formNoValidate(self, value): self._set_bool("formnovalidate", value)
    def get_formTarget(self): return self._attr("formtarget")
    def set_formTarget(self, value): self._set_string("formtarget", value)
    def get_height(self): return self._attr("height")
    def set_height(self, value): self._set_string("height", value)
    def get_width(self): return self._attr("width")
    def set_width(self, value): self._set_string("width", value)
    def get_max(self): return self._attr("max")
    def set_max(self, value): self._set_string("max", value)
    def get_min(self): return self._attr("min")
    def set_min(self, value): self._set_string("min", value)
    def get_multiple(self): return self._bool("multiple")
    def set_multiple(self, value): self._set_bool("multiple", value)
    def get_name(self): return self._attr("name")
    def set_name(self, value): self._set_string("name", value)
    def get_pattern(self): return self._attr("pattern")
    def set_pattern(self, value): self._set_string("pattern", value)
    def get_placeholder(self): return self._attr("placeholder")
    def set_placeholder(self, value): self._set_string("placeholder", value)
    def get_readOnly(self): return self._bool("readonly")
    def set_readOnly(self, value): self._set_bool("readonly", value)
    def get_required(self): return self._bool("required")
    def set_required(self, value): self._set_bool("required", value)
    def get_src(self): return url_value(self, "src")
    def set_src(self, value): set_url_value(self, "src", value)
    def get_step(self): return self._attr("step")
    def set_step(self, value): self._set_string("step", value)
    def get_align(self): return self._attr("align")
    def set_align(self, value): self._set_string("align", value)
    def get_useMap(self): return self._attr("usemap")
    def set_useMap(self, value): self._set_string("usemap", value)
    def get_webkitdirectory(self): return self._bool("webkitdirectory")
    def set_webkitdirectory(self, value): self._set_bool("webkitdirectory", value)
    def get_incremental(self): return self._bool("incremental")
    def set_incremental(self, value): self._set_bool("incremental", value)
    def get_maxLength(self):
        value = self._long('maxlength')
        return value if value is not None and 0 <= value <= 0x7FFFFFFF else -1
    def set_maxLength(self, value):
        self._set_length_bound('maxLength', 'maxlength', value)
    def get_minLength(self):
        value = self._long('minlength')
        return value if value is not None and 0 <= value <= 0x7FFFFFFF else -1
    def set_minLength(self, value):
        self._set_length_bound('minLength', 'minlength', value)
    def _set_length_bound(self, member, attribute, value):
        value = integer(value, bits=32, signed=True)
        if value < 0:
            raise IndexSizeError(f"Failed to set the '{member}' property on 'HTMLInputElement': The provided value ({value}) is not positive or 0.")
        self.attributes[attribute] = str(value)
    def get_size(self):
        parsed = self._long("size")
        return parsed if parsed is not None and parsed > 0 else 20
    def set_size(self, value): self.attributes["size"] = str(integer(value))

    def get_form(self):
        owner = self._form_owner()
        return owner if owner is not None else pyv8.Null
    def _form_owner(self):
        return form_owner(self)
    def get_list(self):
        list_id = self.attributes.get("list")
        if not list_id or not self._services: return pyv8.Null
        found = self._services.document_services.document.fn_getElementById(list_id)
        if isinstance(found, Element) and found.localName == "datalist": return found
        return pyv8.Null
    def get_labels(self):
        return NodeList(owner=self, getter=self._label_elements)
    def _label_elements(self):
        identifier = self.get_id()
        labels = []
        for node in self._document_descendants():
            if not isinstance(node, Element) or node.localName != "label": continue
            if "for" in node.attributes:
                if identifier and node.attributes.get("for") == identifier: labels.append(node)
            elif self._is_descendant_of(node): labels.append(node)
        return labels
    def _document_descendants(self):
        document = self._services.document_services.document if self._services else None
        if document is None: return self._descendants()
        return document._descendants()
    def _is_descendant_of(self, label):
        node = self.parentElement
        while node is not None:
            if node is label: return True
            node = node.parentElement
        return False
    def get_popoverTargetElement(self):
        target = self.attributes.get("popovertarget")
        if not target or not self._services: return pyv8.Null
        found = self._services.document_services.document.fn_getElementById(target)
        return found if isinstance(found, Element) else pyv8.Null
    def set_popoverTargetElement(self, value):
        if is_nullish(value):
            self.attributes.pop("popovertarget", None)
            return
        element = interface_value(value, 'Element', 'Element', 'popoverTargetElement', 0, nullable=True)
        identifier = element.get_id() if element is not None and element is not pyv8.Null else ""
        if identifier: self.attributes["popovertarget"] = str(identifier)
    def get_popoverTargetAction(self):
        value = self._attr("popovertargetaction")
        return value if value in ("click", "hide", "show", "toggle") else "click"
    def set_popoverTargetAction(self, value): self._set_string("popovertargetaction", value)
    def get_webkitEntries(self):
        if self._services is None:
            return []
        # Chrome exposes a fresh frozen array for each read, including when no
        # directory has been selected.
        return self._services.realm_services.v8_context.freeze([])

    def get_willValidate(self):
        if self.get_type() in _BARRED_INPUT_TYPES: return False
        if self._bool("disabled"): return False
        if self._bool("readonly") and self.get_type() in _READONLY_INPUT_TYPES: return False
        node = self.parentElement
        while isinstance(node, Element):
            if node.localName == "datalist": return False
            node = node.parentElement
        return True
    def get_validity(self):
        if self._validity_state is None: self._validity_state = ValidityState(self)
        return self._validity_state
    def get_validationMessage(self): return self._custom_message
    def fn_setCustomValidity(self, message=MISSING, *_extra):
        if message is MISSING:
            raise missing_error('HTMLInputElement', 'setCustomValidity', 1, 0, constructor=False)
        self._custom_message = str(dom_string(message))
    def _validity_flags(self):
        flags = {"customError": bool(self._custom_message)}
        kind, value = self.get_type(), self.get_value()
        if self._bool("required"):
            if kind in ("checkbox", "radio"): flags["valueMissing"] = not self._checked
            else: flags["valueMissing"] = value == ""
        if value:
            if kind == "email":
                parts = value.split(",") if self._bool("multiple") else [value]
                flags["typeMismatch"] = any(not _EMAIL_PATTERN.match(part.strip()) for part in parts)
            elif kind == "url":
                flags["typeMismatch"] = not _URL_PATTERN.match(value)
            flags["patternMismatch"] = (
                "pattern" in self.attributes and not self._pattern_matches(value))
            if self._dirty_value:
                maximum_length, minimum_length = self._long("maxlength"), self._long("minlength")
                flags["tooLong"] = maximum_length is not None and len(value) > maximum_length
                flags["tooShort"] = minimum_length is not None and 0 < len(value) < minimum_length
            if kind in _STEPPABLE_INPUT_TYPES:
                number = self._parse_value(kind, value)
                if number is not None:
                    maximum, minimum = self._parse_value(kind, self._attr("max")), self._parse_value(kind, self._attr("min"))
                    if maximum is not None and number > maximum: flags["rangeOverflow"] = True
                    if minimum is not None and number < minimum: flags["rangeUnderflow"] = True
                    flags["stepMismatch"] = self._step_mismatch(kind, number)
        elif "required" in self.attributes and kind == "file":
            flags["valueMissing"] = True
        return flags
    def _pattern_matches(self, value):
        try: return re.compile(self._attr("pattern")).fullmatch(value) is not None
        except re.error: return True
    def fn_checkValidity(self, *_extra):
        valid = self.get_validity().get_valid()
        if not valid: self._dispatch_invalid()
        return valid
    def fn_reportValidity(self, *_extra):
        valid = self.get_validity().get_valid()
        if not valid: self._dispatch_invalid()
        return valid
    def _dispatch_invalid(self):
        from ..dom.events import Event
        self._dispatch_event(Event("invalid", {"bubbles": False, "cancelable": True}))

    def _require_selectable(self):
        if self.get_type() not in _SELECTABLE_INPUT_TYPES:
            raise InvalidStateError(
                "Failed to execute the selection API on 'HTMLInputElement': the input element's type "
                f"('{self.get_type()}') does not support selection.")
    def _clamp_selection(self, start, end):
        length = len(self.get_value())
        start = max(0, min(int(start), length))
        end = max(0, min(int(end), length))
        return start, max(start, end)
    def get_selectionStart(self):
        self._require_selectable()
        return self._selection_start
    def set_selectionStart(self, value):
        self._require_selectable()
        self._selection_start, self._selection_end = self._clamp_selection(value, self._selection_end)
    def get_selectionEnd(self):
        self._require_selectable()
        return self._selection_end
    def set_selectionEnd(self, value):
        self._require_selectable()
        self._selection_start, self._selection_end = self._clamp_selection(self._selection_start, value)
    def get_selectionDirection(self): return self._selection_direction
    def set_selectionDirection(self, value):
        value = dom_string(value)
        if value not in ("forward", "backward", "none"):
            raise PageTypeError("Failed to set the 'selectionDirection' property on 'HTMLInputElement': "
                               "The provided value is not one of 'forward', 'backward', or 'none'.")
        self._selection_direction = str(value)
    def fn_select(self, *_extra):
        self._require_selectable()
        self._selection_start, self._selection_end = 0, len(self.get_value())
        from ..dom.events import Event
        self._dispatch_event(Event("select", {"bubbles": False, "cancelable": False}))
    def fn_setSelectionRange(self, start=MISSING, end=MISSING, direction="none", *_extra):
        if start is MISSING or end is MISSING:
            raise missing_error('HTMLInputElement', 'setSelectionRange', 2, 0, constructor=False)
        self._require_selectable()
        direction = "none" if is_nullish(direction) else str(dom_string(direction))
        if direction not in ("forward", "backward", "none"):
            raise PageTypeError("Failed to execute 'setSelectionRange' on 'HTMLInputElement': "
                               "The provided value is not one of 'forward', 'backward', or 'none'.")
        self._selection_start, self._selection_end = self._clamp_selection(start, end)
        self._selection_direction = str(direction)
    def fn_setRangeText(self, replacement=MISSING, start=None, end=None, mode="preserve", *_extra):
        if replacement is MISSING:
            raise missing_error('HTMLInputElement', 'setRangeText', 1, 0, constructor=False)
        self._require_selectable()
        replacement = dom_string(replacement)
        mode = "preserve" if is_nullish(mode) else str(mode)
        if mode not in ("preserve", "select", "start", "end"):
            raise PageTypeError("Failed to execute 'setRangeText' on 'HTMLInputElement': "
                               "The selection mode must be 'preserve', 'select', 'start' or 'end'.")
        length = len(self.get_value())
        start = self._selection_start if start is None else int(start)
        end = self._selection_end if end is None else int(end)
        start, end = max(0, min(start, length)), max(0, min(end, length))
        if start > end: raise PageTypeError("Failed to execute 'setRangeText' on 'HTMLInputElement': start > end.")
        value = self.get_value()
        updated = value[:start] + str(replacement) + value[end:]
        self.set_value(updated)
        updated = self.get_value()
        if mode == "select": self._selection_start, self._selection_end = start, start + len(str(replacement))
        elif mode == "start": self._selection_start = self._selection_end = start
        elif mode == "end": self._selection_start = self._selection_end = start + len(str(replacement))
        else: self._selection_start, self._selection_end = self._clamp_selection(
            self._selection_start, self._selection_end)

    def _require_steppable(self):
        if self.get_type() not in _STEPPABLE_INPUT_TYPES:
            raise InvalidStateError("Failed to execute 'stepUp'/'stepDown' on 'HTMLInputElement': "
                                    f"the input element's type ('{self.get_type()}') does not support stepping.")
    def _step_value(self):
        step = self._parse_value(self.get_type(), self._attr("step")) if self._attr("step") != "any" else None
        if step is not None and step > 0: return step
        return {"date": 86400000.0, "week": 604800000.0, "month": 2629800000.0,
                "time": 60000.0, "datetime-local": 60000.0}.get(self.get_type(), 1.0)
    def _step_base(self):
        minimum = self._parse_value(self.get_type(), self._attr("min"))
        if minimum is not None: return minimum
        return 0.0
    def _step_mismatch(self, kind, number):
        if kind not in _STEPPABLE_INPUT_TYPES or self._attr("step") == "any": return False
        step = self._step_value()
        if step <= 0: return False
        offset = (number - self._step_base()) / step
        return abs(offset - round(offset)) > 1e-9
    def _clamp_number(self, kind, candidate):
        maximum, minimum = self._parse_value(kind, self._attr("max")), self._parse_value(kind, self._attr("min"))
        if maximum is not None and candidate > maximum: candidate = maximum
        if minimum is not None and candidate < minimum: candidate = minimum
        return candidate
    def _step(self, direction, count):
        self._require_steppable()
        steps = integer(1 if is_nullish(count) else count) * direction
        kind = self.get_type()
        value = self._parse_value(kind, self.get_value())
        candidate = self._step_base() if value is None else value
        candidate = self._clamp_number(kind, candidate + steps * self._step_value())
        self.set_value(self._format_value(kind, candidate))
    def fn_stepUp(self, count=1, *_extra): self._step(1, count)
    def fn_stepDown(self, count=1, *_extra): self._step(-1, count)

    def _parse_value(self, kind, value):
        """The number the given type reads out of ``value``, or ``None``."""
        value = str(value or "")
        if not value: return None
        if kind in ("number", "range"):
            try: return float(value)
            except ValueError: return None
        if kind in _DATE_INPUT_TYPES:
            return self._parse_date(kind, value)
        return None
    def _parse_date(self, kind, value):
        from datetime import datetime as _datetime
        from datetime import timedelta as _timedelta
        if kind == "week":
            match = re.match(r"^(\d{4})-W(\d{2})$", value)
            if not match: return None
            try:
                week = int(match.group(2))
                if not 1 <= week <= 53: return None
                first = _datetime(int(match.group(1)), 1, 4)
                monday = first - _timedelta(days=first.isoweekday() - 1)
                return monday.timestamp() * 1000.0 + (week - 1) * 604800000.0
            except ValueError: return None
        if kind == "month":
            match = re.match(r"^(\d{4})-(\d{2})$", value)
            if not match: return None
            year, month = int(match.group(1)), int(match.group(2))
            if not 1 <= month <= 12: return None
            return _datetime(year, month, 1).timestamp() * 1000.0
        pattern = _DATE_PATTERNS[kind][0]
        match = re.match(pattern, value)
        if not match: return None
        try:
            numbers = [int(part) for part in match.groups() if part is not None]
            if kind == "date":
                year, month, day = numbers
                if not 1 <= month <= 12 or not 1 <= day <= 31: return None
                return _datetime(year, month, day).timestamp() * 1000.0
            if kind == "time":
                hour, minute = numbers[0], numbers[1]
                second = numbers[2] if len(numbers) > 2 else 0
                if hour > 23 or minute > 59 or second > 59: return None
                return (hour * 3600 + minute * 60 + second) * 1000.0
            year, month, day, hour, minute = numbers[:5]
            second = numbers[5] if len(numbers) > 5 else 0
            if not 1 <= month <= 12 or not 1 <= day <= 31: return None
            if hour > 23 or minute > 59 or second > 59: return None
            return _datetime(year, month, day, hour, minute, second).timestamp() * 1000.0
        except ValueError: return None
    def _format_value(self, kind, number):
        if kind in ("number", "range"): return self._format_number(number)
        from datetime import datetime as _datetime
        moment = _datetime.fromtimestamp(number / 1000.0)
        if kind == "date": return moment.strftime("%Y-%m-%d")
        if kind == "month": return moment.strftime("%Y-%m")
        if kind == "week":
            year, week, _ = moment.isocalendar()
            return f"{year:04d}-W{week:02d}"
        if kind == "time": return moment.strftime("%H:%M:%S") if moment.second or moment.microsecond else moment.strftime("%H:%M")
        return moment.strftime("%Y-%m-%dT%H:%M:%S") if moment.second else moment.strftime("%Y-%m-%dT%H:%M")
    def _format_number(self, number):
        if number == int(number): return str(int(number))
        return repr(number)

    def get_files(self):
        if self.get_type() != "file":
            raise InvalidStateError("Failed to read the 'files' property from 'HTMLInputElement': "
                                    f"the input element's type ('{self.get_type()}') does not support it.")
        from ..fileapi.file_list import FileList  # lazy: the fileapi package imports this one
        if self._files is None: self._files = FileList()
        return self._files
    def set_files(self, value):
        if self.get_type() != "file":
            raise InvalidStateError("Failed to set the 'files' property from 'HTMLInputElement': "
                                    f"the input element's type ('{self.get_type()}') does not support it.")
        from ..fileapi.file_list import FileList  # lazy: the fileapi package imports this one
        if is_nullish(value): self._files = FileList()
        else: self._files = interface_value(value, 'FileList', 'FileList', 'files', 0, nullable=True)
    def fn_showPicker(self, *_extra):
        if self.get_type() not in ("date", "month", "week", "time", "datetime-local", "color", "file", "number"):
            raise NotSupportedError("Failed to execute 'showPicker' on 'HTMLInputElement': "
                                    f"the input element's type ('{self.get_type()}') does not support a picker.")

    def _activatable(self):
        """The input types a click does something to besides firing the event."""
        return self.get_type() in _ACTIVATION_INPUT_TYPES

    def _run_activation(self, event=None):
        """A click toggles a checkable input, or acts on the form it belongs to.  The
        ``input``/``change`` pair a toggled checkbox/radio fires is trusted, as Chromium's."""
        kind = self.get_type()
        trusted = bool(getattr(event, "_trusted", False))
        if kind in ("checkbox", "radio"):
            self._dirty_checked = True
            if getattr(event, "_activation_prepared", None) is not self:
                self.set_checked(not self._checked if kind == "checkbox" else True)
            from ..dom.events import Event
            for event_type, bubbles in (("input", False), ("change", True)):
                event = Event(event_type, {"bubbles": bubbles, "cancelable": False})
                event._trusted = True
                self._dispatch_event(event)
            return
        form = self._form_owner()
        if form is None:
            return
        if kind in ("submit", "image"):
            form._submit(self, user_activated=trusted)
        elif kind == "reset":
            form.fn_reset()
