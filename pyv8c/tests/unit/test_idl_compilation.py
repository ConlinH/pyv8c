import gc
import weakref
from collections import Counter

import pytest

pytest.importorskip("pyv8c.pyv8", reason="native pyv8 extension is not importable")
pytestmark = pytest.mark.native

from pyv8c.engine.bindings import Idl, Bindings  # noqa: E402
from pyv8c.platform.bootstrap import bootstrap_runtime  # noqa: E402
from pyv8c.engine import binder as binder_module  # noqa: E402
from pyv8c.platform.sessions import BindingSession  # noqa: E402


@pytest.fixture
def bindings():
    return Bindings(Idl.load(), runtime=bootstrap_runtime(), stub_logging="off")


def test_generated_classes_are_stable_per_bindings_and_isolated_between_bindings(bindings):
    other = Bindings(Idl.load(), runtime=bootstrap_runtime(), stub_logging="off")
    first = bindings.class_for("AbsoluteOrientationSensor")
    assert bindings.class_for("AbsoluteOrientationSensor") is first
    assert other.class_for("AbsoluteOrientationSensor") is not first


def test_an_unconfigured_bindings_keeps_no_override_table(bindings):
    """Unconfigured hooks cost nothing per member: the binder holds no table at all."""
    assert not bindings.overrides
    assert bindings._binder.overrides is None

    hooked = Bindings(Idl.load(), runtime=bootstrap_runtime(),
                               stub_logging="off",
                               interface_overrides={"Navigator": {"userAgent": lambda *_: "hooked"}})
    assert hooked.overrides
    assert hooked._binder.overrides is not None


def test_compilation_and_signature_analysis_are_not_repeated(bindings, monkeypatch):
    compiler = bindings._binder
    calls = Counter()
    original = compiler._compile

    def compile_once(name):
        calls[name] += 1
        return original(name)

    monkeypatch.setattr(compiler, "_compile", compile_once)
    first = BindingSession(bindings, object()).class_config_for("Element")
    parameters = dict(binder_module._PARAMETERS)
    for _ in range(4):
        assert BindingSession(bindings, object()).class_config_for("Element") is first
    assert all(count == 1 for count in calls.values())
    assert binder_module._PARAMETERS == parameters


def test_descendant_lookup_is_computed_once(bindings):
    table = bindings.idl
    first = table.descendants("EventTarget")
    assert isinstance(first, tuple)
    # The same answer object, not an equal rebuild: one member lookup walks
    # ancestors, and each of them asks for the same descendant set again.
    assert table.descendants("EventTarget") is first
    assert "Element" in first


def test_a_failed_batch_stages_nothing_and_can_be_retried(bindings, monkeypatch):
    session = BindingSession(bindings)
    calls = []
    state = {"failing": True}
    original = bindings.class_config_for

    def bind(name, realm=None):
        calls.append(name)
        if state["failing"] and name == "Node":
            raise RuntimeError("binding failed")
        return original(name, realm)

    monkeypatch.setattr(bindings, "class_config_for", bind)
    with pytest.raises(RuntimeError, match="binding failed"):
        session.materialize("Element")
    assert not session.configs
    assert session.materialized_interfaces == ()

    state["failing"] = False
    session.materialize("Element")
    assert set(session.materialized_interfaces) == {"EventTarget", "Node", "Element"}
    assert set(session.configs) == {bindings.class_for(name) for name in session.materialized_interfaces}

    # Nothing is rebuilt for a chain the realm already staged.
    before = len(calls)
    session.materialize("Element")
    assert len(calls) == before


def test_event_handler_slots_follow_the_baseline_member_table(bindings):
    """An event target states its ``on*`` slots, and the document says which members it has."""
    event_target = bindings.class_for("EventTarget")
    overrides = event_target.__dict__["__idl_overrides__"]["members"]
    assert len(overrides) == len(set(overrides))
    assert {"onload", "onerror", "onclick", "onkeydown", "onhashchange"} <= set(overrides)
    # They are instance members: the document answers them through the objects that carry them.
    assert all(fields == {"location": "instance"} for fields in overrides.values())
    for slot in ("onclick", "onkeydown"):
        assert event_target.__dict__["get_" + slot].__name__ == "get_" + slot
        assert event_target.__dict__["set_" + slot].__name__ == "set_" + slot

    window = {item.name: item for item in bindings.class_config_for("Window").attributes}
    assert {"onclick", "onkeydown", "onwheel", "onhashchange", "onpopstate", "onstorage"} <= set(window)
    assert window["onclick"].getter.__name__ == "get_onclick"

    document = {item.name for item in bindings.class_config_for("Document").attributes}
    assert {"onclick", "onvisibilitychange", "onfreeze", "onresume"} <= document

    # Interfaces whose baseline member table has no such slot never grow one.
    node = {item.name for item in bindings.class_config_for("Node").attributes}
    assert not ({"onclick", "onkeydown", "onhashchange", "onvisibilitychange"} & node)


def test_realm_dependent_callbacks_are_isolated_and_released(bindings):
    class Realm:
        pass

    first_realm, second_realm = Realm(), Realm()
    reference = weakref.ref(first_realm)
    first = BindingSession(bindings, first_realm)
    second = BindingSession(bindings, second_realm)
    first_spec = first.class_config_for("URL")
    second_spec = second.class_config_for("URL")
    assert first_spec is not second_spec
    first_methods = {member.name: member.callback for member in first_spec.methods}
    second_methods = {member.name: member.callback for member in second_spec.methods}
    assert first_methods["createObjectURL"] is not second_methods["createObjectURL"]
    assert first_methods["canParse"] is second_methods["canParse"]
    del first, first_spec, first_methods, first_realm
    gc.collect()
    assert reference() is None


def test_lazy_factory_marks_each_ancestor_from_its_own_spec(bindings):
    session = BindingSession(bindings, object())
    cls, specs, shared = session.lazy_factory()("AnimationEvent")
    assert cls is bindings.class_for("AnimationEvent")
    assert set(specs) == set(shared)
    assert shared["AnimationEvent"] is False
    # Event has a realm-bound constructor. Its child inherits that template.
    assert shared["Event"] is False

    _, _, collection_shared = session.lazy_factory()("HTMLCollection")
    # Its Python mixin is the native template parent but has no staged IDL spec.
    assert collection_shared["HTMLCollection"] is False


def test_strict_validation_is_a_single_cached_audit(bindings, monkeypatch):
    bindings.validate()
    before = dict(bindings._binder._compiled)

    def unexpected(name):
        raise AssertionError(name)

    monkeypatch.setattr(bindings._binder, "_compile", unexpected)
    bindings.validate()
    BindingSession(bindings).materialize("Element")
    assert bindings._binder._compiled == before


def test_a_member_the_convention_names_binds_without_a_declaration(bindings):
    """``fn_<member>`` is the class saying what the IDL cannot: it writes this one.

    ``Element.setAttributeNS`` sits next to its namespace siblings with a body and an arity
    check of its own, and is never declared -- so the convention is what binds it.
    """
    cls = bindings.class_for("Element")
    compiled = bindings._binder.compile("Element")
    methods = {member.name: member.callback for member in compiled.spec.methods}

    assert "setAttributeNS" not in (cls.__dict__.get("__idl_overrides__") or {}),\
        "the convention is what binds it"
    assert methods["setAttributeNS"].__name__ == "fn_setAttributeNS"
    assert not getattr(methods["setAttributeNS"], "__v8_idl_stub__", False)


def test_no_declaration_at_all_still_answers_the_bodies_the_class_writes(bindings, monkeypatch):
    """The end state: nothing stated, and the bodies answer."""
    cls = bindings.class_for("Element")
    monkeypatch.setattr(cls, "__idl_overrides__", {})
    compiled = bindings._binder.compile("Element")
    methods = {member.name: member.callback for member in compiled.spec.methods}
    attributes = {member.name: member for member in compiled.spec.attributes}

    assert methods["setAttribute"].__name__ == "fn_setAttribute"
    assert methods["setAttributeNS"].__name__ == "fn_setAttributeNS"
    # A getter and a writer, both named by the convention and both real callbacks.
    assert attributes["id"].getter.__name__ == "get_id"
    assert attributes["id"].setter.__name__ == "set_id"
    assert attributes["tagName"].getter.__name__ == "get_tagName"
    # A member with no body still keeps its shell.
    assert any(getattr(callback, "__v8_idl_stub__", False) for callback in methods.values())


def test_an_override_wins_over_the_convention(bindings, monkeypatch):
    """An override still decides, which is what the five renamed callbacks rely on."""
    cls = bindings.class_for("Element")
    monkeypatch.setattr(cls, "__idl_overrides__",
                        {"setAttributeNS": {"callback": "fn_setAttribute", "length": 3}})
    compiled = bindings._binder.compile("Element")
    methods = {member.name: member.callback for member in compiled.spec.methods}

    assert methods["setAttributeNS"].__name__ == "fn_setAttribute"


def test_a_static_member_keeps_its_realm_injection_without_a_declaration(bindings, monkeypatch):
    """``_realm`` injection follows the signature, so the convention keeps it too."""
    cls = bindings.class_for("URL")
    monkeypatch.setattr(cls, "__idl_overrides__", {})
    assert bindings._binder.compile("URL").realm_methods, "createObjectURL is a static realm method"

    here = {member.name: member.callback
            for member in BindingSession(bindings, object()).class_config_for("URL").methods}
    plain = {member.name: member.callback for member in bindings.class_config_for("URL").methods}

    assert here["createObjectURL"] is not plain["createObjectURL"]


def test_parameter_names_read_off_the_code_agree_with_a_signature():
    """The wrapper reads ``__code__`` instead of ``inspect.signature``, once per member of every
    interface.  Anything with its own idea of a signature keeps the general answer, so the two
    have to agree on every shape of callable a binding sees."""
    import functools
    import inspect

    from pyv8c.engine.binder import _parameter_names

    def plain(a, b=1, *, c):
        return (a, b, c)

    def wrapped(a):
        return a

    wrapped.__wrapped__ = lambda *args: None

    class Holder:
        def method(self, a, b):
            return (a, b)

        @staticmethod
        def static(a, b):
            return (a, b)

        @classmethod
        def klass(cls, a):
            return a

    stated = lambda *args: None
    stated.__signature__ = inspect.Signature(parameters=[])

    class Callable:
        def __call__(self, a, b):
            return (a, b)

    shapes = (plain, wrapped, Holder.method, Holder().method, Holder.static, Holder.klass,
              functools.partial(plain, 1), Callable(), len, stated)
    for shape in shapes:
        assert _parameter_names(shape) == tuple(inspect.signature(shape).parameters)
