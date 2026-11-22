"""Every declaration in ``__idl_overrides__`` has to say something the IDL does not.

The IDL owns the shape -- which members exist, where they live, their arity and their flags --
and the ``fn_``/``get_``/``set_`` convention answers the members a class implements.  A
declaration that repeats either is noise that can drift, so this freezes what the audit found
when the declaration surface moved to ``__idl_overrides__`` (2026-10-07,
``python -m pyv8c.tools.idl_audit``): each batch of that work lowers a number, and the two in
``NOISE`` end at zero.
"""

import pytest

pytest.importorskip("pyv8c.pyv8", reason="native pyv8 extension is not importable")

from pyv8c.engine.bindings import Bindings  # noqa: E402
from pyv8c.engine.idl import Idl  # noqa: E402
from pyv8c.platform.bootstrap import bootstrap_runtime  # noqa: E402
from pyv8c.tools import idl_audit  # noqa: E402


#: Frozen after the declaration surface moved to ``__idl_overrides__`` (2026-10-07).  ``flags``,
#: ``renamed`` and ``resolved`` are the real overrides -- what a declaration is for: a member the
#: class answers differently, under another name, or installs on an interface the IDL only
#: resolves elsewhere.
#:
#: ``flags`` fell to zero when the last three handler declarations stopped claiming a side effect
#: the document does not state: the flags are the document's, and a class that contradicts them
#: is a change to what V8 may elide, which is not a declaration worth a line.
#:
#: ``restating`` is a residue, not a target met: what is left is the same kind of line in the
#: interfaces whose member list is also a *body* (``ValidityState``'s flags,
#: ``PerformanceTiming``'s timings), where taking the name out of the list would take the
#: implementation with it.  ``DOMException``/``DOMError`` left it when the core feature stopped
#: restating their getters' side effect -- the document already states it, and the compiled spec
#: is unchanged without those lines (measured: two dumps, byte-identical).  The counts below may
#: only fall.
ALLOWED = {
    "restating": 27,
    "flags": 0,
    "renamed": 5,
    "unresolved": 0,
    "resolved": 143,
    "no_interface": 0,
    "declared": 175,
    "specs": 11,
}


#: What may not come back at all: lines that say nothing, and lines that can never install.
NOISE = ("no_interface", "unresolved")


@pytest.fixture(scope="module")
def report():
    return idl_audit.audit(Bindings(Idl.load(), runtime=bootstrap_runtime()))


@pytest.mark.parametrize("bucket", sorted(set(ALLOWED) - {"declared", "specs"}))
def test_the_audit_only_falls(report, bucket):
    """A batch lowers its number; nothing may put it back."""
    assert report["counts"][bucket] <= ALLOWED[bucket], (
        f"{bucket} grew from {ALLOWED[bucket]} to {report['counts'][bucket]}: a declaration is"
        " worth its line only when it differs from the baseline (or the convention cannot"
        " answer it), and the rest belongs in the batches that delete it")


def test_the_set_of_declarations_only_falls(report):
    assert report["declared"] <= ALLOWED["declared"]
    assert report["specs"] <= ALLOWED["specs"]


def test_every_declaration_lands_in_exactly_one_bucket(report):
    """The audit has to see all of them, or a restatement could hide in the ones it skipped."""
    assert sum(report["counts"].values()) == report["declared"]


def test_the_noise_names_the_file_a_batch_has_to_open(report):
    for bucket in NOISE:
        for row in report["rows"][bucket]:
            assert row["file"].endswith(".py"), row
