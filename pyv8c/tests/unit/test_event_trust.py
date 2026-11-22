"""An event the engine raises is trusted; only a script's own event is not."""
import re
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2] / "pyv8c"
#: A class whose name ends in ``Event``, built with a literal lowercase event type.
CONSTRUCTION = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*Event\(\s*[\"'][a-z][a-z0-9-]*[\"']")
#: How far past the construction line the init dictionary may run.
WINDOW = 6
#: Chrome calls the click ``element.click()`` makes untrusted, and this file is where that
#: click is built; every other event in the package is the engine's own.
UNTRUSTED = {"web/dom/activation.py"}


def test_every_event_the_engine_raises_is_marked_trusted():
    """Chrome reports ``isTrusted`` per dispatch, not per class.

    Every construction site in the package is one the engine raises, so each has to say so:
    ``worker.postMessage`` reaching a worker, an ``icecandidate``, or the synthetic ``click``
    an ``element.click()`` makes.  A site that says neither ``isTrusted`` nor ``_trusted``
    answers ``false`` where Chrome answers ``true`` -- which is how the challenge's worker
    used to skip its own payload.
    """
    unmarked = []
    for path in sorted(PACKAGE.rglob("*.py")):
        name = path.relative_to(PACKAGE).as_posix()
        if name in UNTRUSTED:
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            if CONSTRUCTION.search(line) is None:
                continue
            window = "\n".join(lines[index:index + WINDOW])
            if "isTrusted" not in window and "_trusted" not in window:
                unmarked.append(f"{name}:{index + 1}")
    assert unmarked == [], "engine events without a trust marker: " + ", ".join(unmarked)
