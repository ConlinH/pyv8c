"""Member lookup preserves precedence while sharing descendant scans."""

import pytest
from pyv8c.engine.idl import Idl, Profile


def member(name, *, kind="attribute", length=0, main_world=True):
    return {"name": name, "kind": kind, "length": length, "main_world": main_world}


def table(interfaces, profile=None):
    return Idl("test", "test-digest", {}, interfaces, profile=profile)


def test_ancestors_take_precedence_over_descendants():
    view = table(
        {
            "Root": {"members": {"prototype": [member("run", kind="operation", length=1)]}},
            "Middle": {"parent": "Root"},
            "Leaf": {
                "parent": "Middle",
                "members": {"prototype": [member("run", kind="operation", length=2)]},
            },
        }
    )
    assert view.member("Middle", "run", "operation").length == 1
    assert view.member("Leaf", "run", "operation").length == 2


def test_descendants_use_document_order_and_prefer_their_main_world_declaration():
    view = table(
        {
            "Root": {},
            "Deep": {
                "parent": "Later",
                "members": {
                    "prototype": [
                        member("value", length=10, main_world=False),
                        member("value", length=11),
                    ]
                },
            },
            "FirstChild": {
                "parent": "Root",
                "members": {"prototype": [member("value", length=22)]},
            },
            "Later": {"parent": "Root"},
        }
    )
    assert view.member("Root", "value", "attribute").length == 11
    assert view.member("Root", "value", "operation") is None


def test_omitted_owner_member_is_not_restored_by_a_descendant():
    profile = Profile("test", omitted_prototype={"Root": frozenset({"hidden"})})
    view = table(
        {
            "Root": {},
            "Child": {
                "parent": "Root",
                "members": {"prototype": [member("hidden"), member("visible")]},
            },
        },
        profile,
    )
    assert view.member("Root", "hidden", "attribute") is None
    assert view.member("Root", "visible", "attribute").name == "visible"
    assert view.member("Child", "hidden", "attribute").name == "hidden"


def test_descendant_profile_omission_is_specific_to_its_location():
    profile = Profile("test", omitted_prototype={"Child": frozenset({"value"})})
    view = table(
        {
            "Root": {},
            "Child": {
                "parent": "Root",
                "members": {
                    "prototype": [member("value", length=1)],
                    "interface": [member("value", length=2)],
                },
            },
        },
        profile,
    )
    found = view.member("Root", "value", "attribute")
    assert found.length == 2
    assert found.is_static


def test_distinct_missing_names_share_one_descendant_scan(monkeypatch):
    view = table(
        {"Root": {}, "Child": {"parent": "Root", "members": {"prototype": [member("visible")]}}}
    )
    original = view.descendants
    scanned = []

    def descendants(name):
        scanned.append(name)
        return original(name)

    monkeypatch.setattr(view, "descendants", descendants)
    for index in range(100):
        assert view.member("Root", f"missing{index}", "attribute") is None
    assert view.member("Root", "visible", "attribute").name == "visible"
    assert scanned == ["Root"]


@pytest.mark.parametrize("name", ["visible", "missing"])
def test_first_descendant_lookup_is_cached_for_both_hits_and_misses(monkeypatch, name):
    view = table(
        {"Root": {}, "Child": {"parent": "Root", "members": {"prototype": [member("visible")]}}}
    )
    first = view.member("Root", name, "attribute")

    def unexpected(_name):
        raise AssertionError("a cached member lookup walked the hierarchy again")

    monkeypatch.setattr(view, "ancestors", unexpected)
    assert view.member("Root", name, "attribute") is first
