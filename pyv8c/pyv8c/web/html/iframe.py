"""``HTMLIFrameElement``: the frame element and the document it holds.  The
reflected attributes are Chromium's; the resource layer builds the browsing context."""

from pyv8c import pyv8

from ...idl import interface
from ...foundation.arguments import boolean, dom_string
from ...foundation.error_policy import host_boundary
from ...foundation.urls import unloadable_scheme
from ..dom.collections import DOMTokenList
from ..permissions_policy import PermissionsPolicy
from .elements import HTMLElement
from .utils import set_url_value, url_value


_SANDBOX_TOKENS = (
    "allow-downloads", "allow-forms", "allow-modals", "allow-orientation-lock",
    "allow-pointer-lock", "allow-popups", "allow-popups-to-escape-sandbox",
    "allow-presentation", "allow-same-origin", "allow-scripts",
    "allow-storage-access-by-user-activation", "allow-top-navigation",
    "allow-top-navigation-by-user-activation",
    "allow-top-navigation-to-custom-protocols",
)

_REFERRER_POLICIES = (
    "no-referrer", "no-referrer-when-downgrade", "origin",
    "origin-when-cross-origin", "same-origin", "strict-origin",
    "strict-origin-when-cross-origin", "unsafe-url",
)


@interface('HTMLIFrameElement', 'HTMLElement')
class HTMLIFrameElement(HTMLElement):
    def __init__(self, tag_name="iframe", services=None, **kw):
        super().__init__(tag_name, services=services, **kw)
        self._child_services = None

    def _is_connected(self):
        """Whether this element is attached to a Document.  Chromium does not start an
        iframe navigation while detached; the adoption hook creates the context instead.
        ``isConnected`` follows a shadow root to its host, which is how the challenge's
        own widget frame is attached."""
        return bool(self.isConnected)

    def _on_removed_from_document(self):
        if self._child_services is not None:
            self._services.resource_services.discard_iframe_context(self)

    def _attribute_changed(self, name, old, new):
        """Apply the navigation side effects of reflected content attributes."""
        if old == new:
            return
        if name == "name":
            if self._child_services is not None:
                self._child_services.window._window_name = str(new or "")
            return
        if name not in ("src", "srcdoc"):
            return
        if name == "src" and "srcdoc" in self.attributes:
            return
        self._resource_started = False
        if not self._is_connected() or self._services is None:
            return
        resources = self._services.resource_services
        if resources is None:
            return
        self._services.realm_services.post_task(lambda: resources.load_iframe(self)
                                                if self._is_connected() else None)

    def get_src(self): return url_value(self, "src")
    def set_src(self, value):
        set_url_value(self, "src", value)
        self._resource_started = False
        if self._services and self._is_connected() and "srcdoc" not in self.attributes:
            resource_services = self._services.resource_services
            if resource_services is not None:
                #: A scheme a page does not load -- a ``javascript:`` URL is the common one
                #: -- leaves the frame where it was, as a browser leaves it: nothing was
                #: asked for, so there is no outcome to report.  No address at all is not
                #: that case: the frame still loads the empty document it starts with.
                target = url_value(self, "src")
                if target and unloadable_scheme(target) is not None:
                    return
                #: A load the host will not make is the host's own outcome: the frame
                #: stays where it was and the reason is logged, the way a browser leaves
                #: the assignment alone rather than failing the script that made it.
                with host_boundary("HTMLIFrameElement.src"):
                    resource_services.load_iframe(self)
    def get_srcdoc(self): return self.attributes.get("srcdoc", "")
    def set_srcdoc(self, value):
        from ..bom.trusted_types import trusted_string
        markup = trusted_string(self._services, value, 'HTML', 'HTMLIFrameElement srcdoc')
        old = self.attributes.get("srcdoc")
        self.attributes["srcdoc"] = markup
        self._resource_started = False
        if self._services is not None and self._is_connected() and old != markup:
            resource_services = self._services.resource_services
            if resource_services is not None:
                self._services.realm_services.post_task(
                    lambda: resource_services.load_iframe_markup(self, markup))
    def get_name(self): return self.attributes.get("name", "")
    def set_name(self, value):
        self.attributes["name"] = str(dom_string(value))
        if self._child_services is not None:
            self._child_services.window._window_name = str(self.attributes["name"])
    def get_sandbox(self):
        tokens = getattr(self, "_sandbox_tokens", None)
        if tokens is None: tokens = self._sandbox_tokens = DOMTokenList(self, "sandbox", _SANDBOX_TOKENS)
        return tokens
    def set_sandbox(self, value):
        """Chrome declares ``sandbox`` with ``PutForwards=value``, so assigning to the
        property writes the content attribute through the token list."""
        self.get_sandbox().set_value(value)
    def get_allow(self): return self.attributes.get("allow", "")
    def set_allow(self, value): self.attributes["allow"] = str(dom_string(value))
    def get_allowFullscreen(self): return "allowfullscreen" in self.attributes
    def set_allowFullscreen(self, value):
        if boolean(value): self.attributes["allowfullscreen"] = ""
        else: self.attributes.pop("allowfullscreen", None)
    def get_width(self): return self.attributes.get("width", "")
    def set_width(self, value): self.attributes["width"] = str(dom_string(value))
    def get_height(self): return self.attributes.get("height", "")
    def set_height(self, value): self.attributes["height"] = str(dom_string(value))
    def get_referrerPolicy(self):
        policy = str(self.attributes.get("referrerpolicy", ""))
        return policy if policy in _REFERRER_POLICIES else ""
    def set_referrerPolicy(self, value):
        policy = str(dom_string(value))
        self.attributes["referrerpolicy"] = policy if policy in _REFERRER_POLICIES else ""
    def get_loading(self):
        loading = str(self.attributes.get("loading", ""))
        return loading if loading in ("lazy", "eager") else "auto"
    def set_loading(self, value):
        loading = str(dom_string(value))
        self.attributes["loading"] = loading if loading in ("lazy", "eager") else "auto"
    def _string_attribute(self, name, value=None):
        if value is None:
            return str(self.attributes.get(name, ""))
        self.attributes[name] = str(dom_string(value))
    def _boolean_attribute(self, name, value):
        if boolean(value):
            self.attributes[name] = ""
        else:
            self.attributes.pop(name, None)
    def get_align(self): return self._string_attribute("align")
    def set_align(self, value): self._string_attribute("align", value)
    def get_scrolling(self): return self._string_attribute("scrolling")
    def set_scrolling(self, value): self._string_attribute("scrolling", value)
    def get_frameBorder(self): return self._string_attribute("frameborder")
    def set_frameBorder(self, value): self._string_attribute("frameborder", value)
    def get_longDesc(self): return url_value(self, "longdesc")
    def set_longDesc(self, value): set_url_value(self, "longdesc", value)
    def get_marginHeight(self): return self._string_attribute("marginheight")
    def set_marginHeight(self, value): self._string_attribute("marginheight", value)
    def get_marginWidth(self): return self._string_attribute("marginwidth")
    def set_marginWidth(self, value): self._string_attribute("marginwidth", value)
    def get_csp(self): return self._string_attribute("csp")
    def set_csp(self, value): self._string_attribute("csp", value)
    def _secure_context(self):
        """Whether this element's document is in a secure context.

        Chrome exposes privateToken, browsingTopics and adAuctionHeaders only there:
        outside one they read as undefined and nothing lands in the attributes.
        """
        from ..bom.utils import is_secure_context
        return is_secure_context(str(getattr(self.ownerDocument, "url", "") or ""))
    def get_privateToken(self):
        return (self._string_attribute("privatetoken") if self._secure_context()
                else pyv8.Undefined)
    def set_privateToken(self, value):
        if self._secure_context():
            self._string_attribute("privatetoken", value)
    def get_credentialless(self): return "credentialless" in self.attributes
    def set_credentialless(self, value): self._boolean_attribute("credentialless", value)
    def get_allowPaymentRequest(self): return "allowpaymentrequest" in self.attributes
    def set_allowPaymentRequest(self, value):
        self._boolean_attribute("allowpaymentrequest", value)
    def get_browsingTopics(self):
        return ("browsingtopics" in self.attributes if self._secure_context()
                else pyv8.Undefined)
    def set_browsingTopics(self, value):
        if self._secure_context():
            self._boolean_attribute("browsingtopics", value)
    def get_adAuctionHeaders(self):
        return ("adauctionheaders" in self.attributes if self._secure_context()
                else pyv8.Undefined)
    def set_adAuctionHeaders(self, value):
        if self._secure_context():
            self._boolean_attribute("adauctionheaders", value)
    def get_featurePolicy(self):
        policy = getattr(self, "_feature_policy", None)
        if policy is None:
            document = self.ownerDocument
            url = document.get_URL() if document is not None else ""
            policy = self._feature_policy = PermissionsPolicy(url=url)
        return policy
    def fn_getSVGDocument(self, *_args): return pyv8.Null
    def get_contentDocument(self):
        """The child's own document object, or null while it is cross-origin."""
        if self._child_services is None:
            return pyv8.Null
        from ..bom.window import Window
        accessor = Window._accessor_window(self._services.document_services.window)
        if accessor._origin != self._child_services._origin:
            return pyv8.Null
        document = self._child_services.document_services.document
        # The child's own object: ``contentDocument`` and ``contentWindow.document`` are one.
        realm = getattr(self._child_services, "realm_services", None)
        v8_context = getattr(realm, "v8_context", None)
        value = None if v8_context is None else v8_context.js_obj(document)
        if value is None and v8_context is not None:
            # The first reader may be the parent: build the wrapper in the child's realm.
            v8_context.to_js(document)
            value = v8_context.js_obj(document)
        return document if value is None else value
    def get_contentWindow(self):
        if self._child_services is None:
            return pyv8.Null
        child_window = self._child_services.document_services.window
        from ..bom.window import Window
        parent_window = Window._accessor_window(self._services.document_services.window)
        return parent_window._view(child_window)

__all__ = ["HTMLIFrameElement"]
