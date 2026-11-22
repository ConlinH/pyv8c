"""Declaration reuse follows the declaring interface and the selected IDL view."""

import gc
import sys
import weakref

import pytest

pytest.importorskip("pyv8c.pyv8")
pytestmark = pytest.mark.native

from pyv8c.engine.idl import Idl, Profile
from pyv8c.idl.spec import idl_spec, interface, members_of, unresolved_members


def view(profile=None):
    return Idl(
        "test",
        "same-document",
        {},
        {
            "Root": {"members": {"prototype": [{"kind": "attribute", "name": "value"}]}},
            "First": {"parent": "Root"},
            "Second": {"parent": "Root"},
        },
        profile=profile,
    )


@interface("Root")
class Root:
    __idl_overrides__ = {"value": {"getter": "get_value"}}


def test_registered_base_reuses_its_declaration_for_all_descendants():
    table = view()
    assert members_of(Root, table, "First") is members_of(Root, table, "Second")
    assert idl_spec(Root, table, "First") is idl_spec(Root, table, "Root")
    assert members_of(Root, table, "First")[1][0].name == "value"


@pytest.mark.parametrize("hidden_first", [False, True])
def test_profile_views_never_share_visible_or_unresolved_declarations(hidden_first):
    visible = view()
    hidden = view(Profile("test", omitted_prototype={"Root": frozenset({"value"})}))
    for table in ([hidden, visible] if hidden_first else [visible, hidden]):
        expected = () if table is hidden else ("value",)
        assert tuple(attribute.name for attribute in members_of(Root, table, "Root")[1]) == expected
        assert (
            tuple(attribute.name for attribute in idl_spec(Root, table, "Root").attributes)
            == expected
        )
        assert unresolved_members(Root, table, "Root") == (
            (("Root", "value"),) if table is hidden else ()
        )


def test_unregistered_mixin_keeps_its_target_interface():
    class Mixin:
        __idl_overrides__ = {"run": {"callback": "fn_run"}}

    table = Idl(
        "test",
        "mixin-document",
        {},
        {
            "First": {
                "members": {"prototype": [{"kind": "operation", "name": "run", "length": 1}]}
            },
            "Second": {
                "members": {"prototype": [{"kind": "operation", "name": "run", "length": 2}]}
            },
        },
    )
    assert members_of(Mixin, table, "First")[0][0].length == 1
    assert members_of(Mixin, table, "Second")[0][0].length == 2


def test_replacing_a_declaration_invalidates_its_member_and_native_specs(monkeypatch):
    table = view()
    first = idl_spec(Root, table, "Root")
    monkeypatch.setattr(Root, "__idl_overrides__", {"value": {"getter": "get_other"}})
    second = idl_spec(Root, table, "Root")
    assert second is not first
    assert second.attributes[0].getter == "get_other"


def test_declaration_caches_do_not_retain_discarded_idl_views():
    table = view()
    reference = weakref.ref(table)
    idl_spec(Root, table, "Root")
    unresolved_members(Root, table, "Root")
    del table
    gc.collect()
    assert reference() is None


async def test_cold_compile_starts_with_fresh_caches_on_every_run(monkeypatch):
    monkeypatch.setattr(sys, "path", sys.path.copy())
    from benchmarks.benchmark import case_cold_compile

    for _ in range(2):
        result = await case_cold_compile()
        assert result["compiled_misses"] == result["interfaces"] > 0
