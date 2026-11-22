"""The override table: what a host may answer for, and what is refused outright.

Nothing here needs the native extension: the table is plain data and the checks are
answered from the interface baseline, so a configuration mistake fails while the
Runtime is being built rather than after a page has run.
"""
import pytest

from pyv8c.engine.idl import Idl
from pyv8c.engine.overrides import MemberOverrides


@pytest.fixture(scope="module")
def table():
    return Idl.load(profile=None)


def hook(*_args):
    return None


class _Member:
    def __init__(self, kind, setter_state=None):
        self.kind = kind
        self.setter_state = setter_state


class _Table:
    """The smallest thing the parser asks of a collection: interfaces and members."""

    def __init__(self, members):
        self.interfaces = {"Fake": {}}
        self._members = members

    def member(self, name, public_name, kind):
        return self._members.get((name, public_name, kind))


async def test_a_bare_callable_answers_the_slot_that_member_is_read_in(table):
    overrides = MemberOverrides.parse(
        {"AbortController": {"abort": hook}, "Navigator": {"hardwareConcurrency": hook}}, table)
    assert overrides.for_member("AbortController", "abort").call is hook
    assert overrides.for_member("AbortController", "abort").get is None
    assert overrides.for_member("Navigator", "hardwareConcurrency").get is hook
    assert overrides.interfaces == frozenset({"AbortController", "Navigator"})
    assert bool(overrides) is True


async def test_named_slots_are_kept_as_given(table):
    overrides = MemberOverrides.parse(
        {"Navigator": {"hardwareConcurrency": {"get": hook},
                       "userAgent": {"get": hook}}}, table)
    assert overrides.for_member("Navigator", "hardwareConcurrency").get is hook
    assert overrides.for_member("Navigator", "userAgent").set is None


async def test_a_setter_is_allowed_where_the_baseline_does_not_call_the_member_read_only(table):
    overrides = MemberOverrides.parse(
        {"HTMLInputElement": {"valueAsNumber": {"get": hook, "set": hook}}}, table)
    member = overrides.for_member("HTMLInputElement", "valueAsNumber")
    assert member.get is hook and member.set is hook


async def test_nothing_configured_answers_nothing(table):
    overrides = MemberOverrides.parse(None, table)
    assert bool(overrides) is False
    assert overrides.interfaces == frozenset()
    assert overrides.for_member("Navigator", "userAgent") is None
    assert overrides.for_member("NotAnInterface", "userAgent") is None


async def test_an_unknown_interface_is_refused(table):
    with pytest.raises(TypeError, match="not in the Chromium interface baseline"):
        MemberOverrides.parse({"Navigatorr": {"userAgent": hook}}, table)


async def test_an_unknown_member_is_refused(table):
    with pytest.raises(TypeError, match="is not a member of Navigator"):
        MemberOverrides.parse({"Navigator": {"userAgentt": hook}}, table)


async def test_a_slot_the_member_kind_cannot_answer_is_refused(table):
    with pytest.raises(TypeError, match="is a operation and answers call"):
        MemberOverrides.parse({"AbortController": {"abort": {"get": hook}}}, table)
    with pytest.raises(TypeError, match="is a attribute and answers get, set"):
        MemberOverrides.parse({"Navigator": {"userAgent": {"call": hook}}}, table)


async def test_a_constant_is_refused():
    fake = _Table({("Fake", "MAX", "constant"): _Member("constant")})
    with pytest.raises(TypeError, match="is a constant"):
        MemberOverrides.parse({"Fake": {"MAX": hook}}, fake)


async def test_a_setter_for_a_member_the_baseline_calls_read_only_is_refused(table):
    with pytest.raises(TypeError, match="is read-only in the interface baseline"):
        MemberOverrides.parse({"Navigator": {"userAgent": {"set": hook}}}, table)


async def test_a_bound_method_is_refused(table):
    class Answer:
        def call(self, *_args):
            return None

    with pytest.raises(TypeError, match="bound method"):
        MemberOverrides.parse({"AbortController": {"abort": Answer().call}}, table)


async def test_something_that_cannot_be_called_is_refused(table):
    with pytest.raises(TypeError, match="is not callable"):
        MemberOverrides.parse({"AbortController": {"abort": 7}}, table)


async def test_a_member_given_no_hook_at_all_is_refused(table):
    with pytest.raises(TypeError, match="was given no hook"):
        MemberOverrides.parse({"Navigator": {"userAgent": {}}}, table)


async def test_a_configuration_that_is_not_a_mapping_is_refused(table):
    with pytest.raises(TypeError, match="must be a mapping of interface name"):
        MemberOverrides.parse(["Navigator"], table)
    with pytest.raises(TypeError, match=r"interface_overrides\['Navigator'\] must map"):
        MemberOverrides.parse({"Navigator": ["userAgent"]}, table)
