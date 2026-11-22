"""Text layout, this engine beside the local Chrome: same pages, same readouts.

Two differences are the machine's and are reported as expected rather than hidden: a CJK run
is drawn by a fallback face each browser picks for itself, and glyph ink is hinted by the
platform Chrome runs on.

Usage::

    python tools/collect_text_layout.py                 # the local Chrome's readings
    python tools/collect_text_layout.py --compare       # both, one line per difference
    python tools/collect_text_layout.py --output base.json

``--compare`` keeps what it read in ``tools/text-layout-baseline.json``, not a shipped input.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))

from pyv8c.tools.collect_environment import (  # noqa: E402
    _local_origin, _navigate, _session, find_chrome, read_devtools_port,
)

DEFAULT_OUTPUT = PACKAGE_ROOT / "tools" / "text-layout-baseline.json"

#: The faces a line height is checked across, and the sizes it is checked at.
FACES = ("Arial", "Times New Roman", "Segoe UI", "Verdana", "Consolas", "Tahoma")
SIZES = (13.3333, 16, 24, 32, 36, 48, 64)
CANVAS_TEXTS = ("AV", "fi", "mmmmmmmmmm", "hello world", "word word")

#: One page and one script per case; the script answers a list of numbers.
CASES = (
    ("wrap-100px",
     "<div id=t style='width:100px;font-family:Arial;font-size:16px'>" + "word " * 60 + "</div>",
     "[t.offsetHeight, t.scrollWidth, t.scrollHeight]"),
    ("wrap-nowrap",
     "<div id=t style='width:100px;font-family:Arial;font-size:16px;white-space:nowrap'>"
     + "word " * 60 + "</div>",
     "[t.offsetHeight, t.scrollWidth]"),
    ("nbsp",
     "<div id=t style='width:40px;font-family:Arial;font-size:16px'>a\u00a0b c d e f</div>",
     "[t.offsetHeight]"),
    ("pre",
     "<div id=t style='width:200px;font-family:Arial;font-size:16px;white-space:pre'>"
     "a\nb\nc</div>",
     "[t.offsetHeight]"),
    ("pre-line",
     "<div id=t style='width:200px;font-family:Arial;font-size:16px;white-space:pre-line'>"
     "a   b\nc</div>",
     "[t.offsetHeight]"),
    ("break-all",
     "<div id=t style='width:20px;font-family:Arial;font-size:16px;word-break:break-all'>"
     "mmmmmmmm</div>",
     "[t.offsetHeight, t.scrollWidth]"),
    ("break-word",
     "<div id=t style='width:60px;font-family:Arial;font-size:16px;overflow-wrap:break-word'>"
     "hi mmmmmmmmmm</div>",
     "[t.offsetHeight, t.scrollWidth]"),
    ("letter-spacing",
     "<span id=t style='display:inline-block;font-family:Arial;font-size:16px;"
     "letter-spacing:2px'>abc</span>",
     "[t.offsetWidth]"),
    ("inline-block",
     "<div style='width:400px'><span id=t style='display:inline-block;font-family:Arial;"
     "font-size:16px'>hello world</span></div>",
     "[t.offsetWidth, t.offsetHeight]"),
    ("inline-block-wrap",
     "<div id=p style='width:120px;font-family:Arial;font-size:16px'>a "
     "<span id=t style='display:inline-block;width:100px'>b</span> c</div>",
     "[Math.round(t.getBoundingClientRect().top), p.offsetHeight]"),
    ("cjk",
     "<div id=t style='width:40px;font-family:Arial;font-size:16px'>"
     "\u4e2d\u6587\u3002\u5b57\u4e5f\u4e0d\u5c11</div>",
     "[t.offsetHeight, t.scrollWidth]"),
    ("cjk-punct",
     "<div id=t style='width:16px;font-family:Arial;font-size:16px'>\u4e2d\u3002</div>",
     "[t.offsetHeight]"),
    ("br",
     "<div id=t style='font-family:Arial;font-size:16px'>a<br>b</div>",
     "[t.offsetHeight]"),
    ("pre-blank-line",
     "<div id=t style='width:200px;font-family:Arial;font-size:16px;white-space:pre'>"
     "a\n\nb</div>",
     "[t.offsetHeight]"),
    ("br-leading",
     "<div id=t style='font-family:Arial;font-size:16px'><br>b</div>",
     "[t.offsetHeight]"),
    ("text-align-right",
     "<div style='width:100px;font-family:Arial;font-size:16px;text-align:right'>"
     "<span id=t style='display:inline-block;width:20px'>a</span></div>",
     "[Math.round(t.getBoundingClientRect().left)]"),
    ("text-align-center",
     "<div style='width:100px;font-family:Arial;font-size:16px;text-align:center'>"
     "<span id=t style='display:inline-block;width:20px'>a</span></div>",
     "[Math.round(t.getBoundingClientRect().left)]"),
    ("word-spacing",
     "<span id=t style='display:inline-block;font-family:Arial;font-size:16px;word-spacing:10px'>"
     "a b</span>",
     "[t.offsetWidth]"),
    ("flex-overflow",
     "<div id=t style='display:flex;width:40px;font-family:Arial;font-size:16px'>"
     "<span style='white-space:nowrap'>wordwordword</span></div>",
     "[t.scrollWidth, t.offsetHeight]"),
)

#: The scripts that are not a single case: line heights, the ways CSS writes one, the canvas.
FACE_SCRIPT = """(() => {
  const out = {};
  for (const face of %s) {
    out[face] = {};
    for (const size of %s) {
      const box = document.createElement('div');
      box.id = 't';
      box.style.cssText = 'font-family:' + face + ';font-size:' + size + 'px;line-height:normal';
      box.textContent = 'x';
      document.body.appendChild(box);
      out[face][size] = box.getBoundingClientRect().height;
      box.remove();
    }
  }
  return out;
})()"""

STYLE_SCRIPT = """(() => {
  const out = {};
  for (const declared of ['normal', '1.5', '150%', '30px', '2em']) {
    const box = document.createElement('div');
    box.id = 't';
    box.style.cssText = 'font-family:Arial;font-size:16px;line-height:' + declared;
    box.textContent = 'x';
    document.body.appendChild(box);
    out[declared] = box.getBoundingClientRect().height;
    box.remove();
  }
  return out;
})()"""

CANVAS_SCRIPT = """(() => {
  const ctx = document.createElement('canvas').getContext('2d');
  ctx.font = '16px Arial';
  const out = {widths: {}, ink: {}, font: {}};
  for (const text of %s) {
    const metrics = ctx.measureText(text);
    out.widths[text] = metrics.width;
    out.ink[text] = [metrics.actualBoundingBoxLeft, metrics.actualBoundingBoxRight,
                     metrics.actualBoundingBoxAscent, metrics.actualBoundingBoxDescent];
  }
  const one = ctx.measureText('x');
  out.font = {ascent: one.fontBoundingBoxAscent, descent: one.fontBoundingBoxDescent};
  return out;
})()"""

#: How far apart two readings may be and still be the same measurement; box heights and scroll
#: extents are whole numbers on both sides, so they must agree exactly.
TOLERANCE = 0.01

#: The differences that belong to the machine, by the reading's name.  Only an unexpected one
#: makes the command fail.
KNOWN = (
    ("cjk.", "a CJK run is drawn by a fallback face the two browsers choose for themselves; "
             "the advances agree and that face's line height does not"),
    ("cjk-punct.", "the same, for the line a closing punctuation mark ends"),
    ("canvas.ink.", "glyph ink is hinted by the platform Chrome runs on, not by this engine, "
                    "and hinting is on the not-aligned list"),
)


def _script(html, readout):
    """One case as a page and a script, with ``t`` bound to the box the case names."""
    return ("(() => { document.body.style.margin = '0';"
            " document.body.innerHTML = %s;"
            " const t = document.getElementById('t');"
            " return %s; })()" % (json.dumps(html), readout))


def collect(cases, *, timeout=60.0, headless=True):
    """The readings the local Chrome gives for *cases*, one entry per case."""
    chrome = find_chrome()
    profile = Path(tempfile.mkdtemp(prefix="text-layout-"))
    arguments = [str(chrome), "--remote-debugging-port=0", f"--user-data-dir={profile}",
                 "--no-first-run", "--no-default-browser-check", "--disable-gpu"]
    if headless:
        arguments.append("--headless=new")
    arguments.append("about:blank")
    process = subprocess.Popen(arguments, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    server = session = None
    try:
        port, _path = read_devtools_port(profile, timeout=timeout)
        server, url = _local_origin()
        session, version = _session(port, timeout)
        _navigate(session, url, timeout)
        return {"browser": version, "cases": _run(session, cases)}
    finally:
        if session is not None:
            session.close()
        if server is not None:
            server.shutdown()
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


def _run(session, cases):
    """Ask *cases* of one session, which may be the reference or this engine."""
    answers = {}
    for name, html, readout in cases:
        answers[name] = session.evaluate(_script(html, readout))
    answers["line-heights"] = session.evaluate(FACE_SCRIPT % (json.dumps(list(FACES)),
                                                              json.dumps(list(SIZES))))
    answers["line-height-styles"] = session.evaluate(STYLE_SCRIPT)
    answers["canvas"] = session.evaluate(CANVAS_SCRIPT % json.dumps(list(CANVAS_TEXTS)))
    return answers


async def _run_here(cases, idl_path=None):
    """The same readings from pyv8c, through the same scripts."""
    from loguru import logger

    from pyv8c.async_api import Browser, run

    logger.remove()
    browser = (await Browser(cache=False, stub_logging="off", real_network=False, idl_path=idl_path).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="https://text-layout.test/"))
        (await page.wait_for_load_state('load', timeout=2))
        answers = {}
        for name, html, readout in cases:
            (await page.set_content("<body></body>", url="https://text-layout.test/"))
            (await page.wait_for_load_state('load', timeout=2))
            answers[name] = page.evaluate(_script(html, readout))
        (await page.set_content("<body></body>", url="https://text-layout.test/"))
        answers["line-heights"] = page.evaluate(FACE_SCRIPT % (json.dumps(list(FACES)),
                                                               json.dumps(list(SIZES))))
        answers["line-height-styles"] = page.evaluate(STYLE_SCRIPT)
        answers["canvas"] = page.evaluate(CANVAS_SCRIPT % json.dumps(list(CANVAS_TEXTS)))
        return {"browser": "pyv8c", "cases": answers}
    finally:
        (await browser.close())


def _flatten(value, prefix=""):
    """Every number of a reading as ``key -> number``, which is what a difference is over."""
    if isinstance(value, dict):
        found = {}
        for name, inner in value.items():
            found.update(_flatten(inner, f"{prefix}{name}."))
        return found
    if isinstance(value, list):
        found = {}
        for index, inner in enumerate(value):
            found.update(_flatten(inner, f"{prefix}{index}."))
        return found
    return {prefix.rstrip("."): value}


def differences(reference, mine, tolerance=TOLERANCE):
    """``(key, reference, mine)`` for every reading the two do not share."""
    found = []
    for key, theirs in _flatten(reference).items():
        ours = _flatten(mine).get(key)
        if isinstance(theirs, (int, float)) and isinstance(ours, (int, float)):
            if abs(float(theirs) - float(ours)) > tolerance:
                found.append((key, theirs, ours))
    return found


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--compare", action="store_true",
                        help="run this engine too and print one line per difference")
    parser.add_argument("--output", type=Path, default=None,
                        help=f"write the reference readings as JSON (default: {DEFAULT_OUTPUT})")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--headful", action="store_true", help="run Chrome with a window")
    parser.add_argument("--idl", type=Path, default=None,
                        help="the IDL zip this engine reads (default: the packaged baseline)")
    arguments = parser.parse_args(argv)
    idl_path = arguments.idl

    cases = list(CASES)
    reference = collect(cases, timeout=arguments.timeout, headless=not arguments.headful)
    print(f"reference: {reference['browser']}")
    destination = arguments.output if arguments.output is not None else (
        DEFAULT_OUTPUT if arguments.compare else None)
    if destination is not None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(reference, indent=1, ensure_ascii=False),
                               encoding="utf-8")
        print(f"wrote {destination}")
    if not arguments.compare:
        for name, value in reference["cases"].items():
            if name in ("line-heights", "canvas"):
                continue
            print(f"  {name:20s} {value}")
        return 0

    mine = (run(_run_here(cases, idl_path)))
    found = differences(reference["cases"], mine["cases"])
    unexpected = 0
    for key, theirs, ours in found:
        reason = next((reason for prefix, reason in KNOWN if key.startswith(prefix)), None)
        mark = "expected" if reason else "DIFFERENT"
        print(f"  {key:44s} chrome={theirs!s:>12s}  pyv8c={ours!s:>12s}  {mark}")
        if reason:
            print(f"      {reason}")
        else:
            unexpected += 1
    total = len(_flatten(reference["cases"]))
    print(f"{len(found)} differences over {total} readings, {unexpected} of them unexpected")
    return 1 if unexpected else 0


from pyv8c.async_api import Browser, run

if __name__ == "__main__":
    raise SystemExit(main())
