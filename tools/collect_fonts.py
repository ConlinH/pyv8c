"""Read what a real Chrome answers about this machine's fonts, and compare it with ours.

A page can ask two things of a font, and an engine that answers differently is visible to
it: whether ``local()`` finds a name, and which platform font a family actually resolves
to.  This reads both from Chrome over DevTools -- the same launcher and session the
environment collector uses -- and writes them to ``tools/fonts-baseline.json``, the baseline
``--compare`` measures this engine against.  That file is one machine's reading, not a shipped
input, and it stays out of the tree.

The names probed are fixed rather than taken from a machine, so two runs on two machines
stay comparable: they cover the three desktop font sets a challenge would use to tell
platforms apart, plus families no machine has.

``queryLocalFonts()`` is deliberately not part of the baseline: it needs a granted
permission and a user gesture, which a headless run cannot supply, and it is the same
table ``local()`` already shows.  To read it by hand, run Chrome with a visible window and
grant ``local-fonts`` once.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from pyv8c.tools.collect_environment import (  # noqa: E402
    CdpSession, _http_json, _local_origin, _navigate, _session, find_chrome,
    read_devtools_port,
)

DEFAULT_OUTPUT = PACKAGE_ROOT / "tools" / "fonts-baseline.json"

#: One name per interesting answer: installed everywhere, installed on one platform only,
#: a PostScript spelling and a full-name spelling of one face, and two nobody has.
PROBE_NAMES = (
    "Arial", "ArialMT", "Times New Roman", "Courier New", "Segoe UI", "Consolas",
    "Segoe UI Emoji", "Cambria Math", "CambriaMath", "Microsoft YaHei", "SimSun",
    "Malgun Gothic", "MS Gothic", "Meiryo", "Segoe UI Historic",
    "Helvetica", "Geneva", "Monaco", "Menlo", "Apple Symbols", "PingFang SC",
    "DejaVu Sans", "Liberation Serif", "Noto Sans", "Noto Sans CJK SC", "Cantarell",
    "Adwaita Sans", "FreeSans", "Ubuntu", "Droid Sans", "Carlito", "Caladea",
    "Arimo", "Tinos", "Cousine", "Roboto", "Open Sans", "Lato", "Fira Sans",
    "Nonexistent Font 123", "Definitely Not A Font",
)

#: The families whose resolved platform font is worth recording, by PostScript name.
PROBE_FAMILIES = ("Arial", "Times New Roman", "Courier New", "Segoe UI", "Cambria Math",
                  "Malgun Gothic", "Nonexistent Font 123")

LOCAL_BITS = """
(async () => {
  const names = %s;
  const answer = {};
  await Promise.all(names.map(async (name) => {
    try {
      const face = await new FontFace(name, 'local("' + name + '")').load();
      answer[name] = { ok: true, status: face.status };
    } catch (exc) {
      answer[name] = { ok: false, name: exc.name, code: exc.code, message: exc.message };
    }
  }));
  return answer;
})()
"""

PLATFORM_FONTS = """
(() => {
  const families = %s;
  const host = document.createElement('div');
  host.style.cssText = 'font-size:16px';
  const seen = [];
  for (const family of families) {
    const child = document.createElement('span');
    child.id = 'probe-' + seen.length;
    child.style.fontFamily = '"' + family + '"';
    child.textContent = 'Wg';
    host.appendChild(child);
    seen.push(family);
  }
  document.body.appendChild(host);
  return seen;
})()
"""


def _probe_local_bits(session):
    """``name -> answer`` for every probed name, as the page reported it."""
    names = json.dumps(list(PROBE_NAMES))
    return session.evaluate(LOCAL_BITS % names)


def _probe_platform_fonts(session):
    """``family -> platform font names``, which is what ``local()`` matches against."""
    session.call("DOM.enable")
    session.call("CSS.enable")
    families = json.dumps(list(PROBE_FAMILIES))
    session.evaluate(PLATFORM_FONTS % families)
    document = session.call("DOM.getDocument", depth=-1)
    root = document["root"]["nodeId"]
    resolved = {}
    for index, family in enumerate(PROBE_FAMILIES):
        found = session.call("DOM.querySelector", nodeId=root, selector=f"#probe-{index}")
        node = found.get("nodeId", 0)
        if not node:
            resolved[family] = []
            continue
        used = session.call("CSS.getPlatformFontsForNode", nodeId=node)
        resolved[family] = sorted({entry.get("postScriptName", "")
                                   for entry in used.get("fonts", [])} - {""})
    return resolved


def collect(*, timeout=60.0, headless=True):
    """Read the baseline from a Chrome this function starts and stops."""
    chrome = find_chrome()
    profile = Path(tempfile.mkdtemp(prefix="font-baseline-"))
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
        return {
            "schema": 1,
            "source": {
                "browser": version,
                "mode": "headless" if headless else "visible",
                "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
            "local": _probe_local_bits(session),
            "platform_fonts": _probe_platform_fonts(session),
        }
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


ANSWER = """
(async () => {
  const names = %s;
  const answer = {};
  await Promise.all(names.map(async (name) => {
    try {
      await new FontFace(name, 'local("' + name + '")').load();
      answer[name] = true;
    } catch (exc) {
      answer[name] = false;
    }
  }));
  return JSON.stringify(answer);
})()
"""


async def compare(baseline):
    """``(name, chrome, ours)`` for every probed name the two engines disagree on."""
    from loguru import logger

    from pyv8c.async_api import Browser, run

    logger.remove()
    browser = (await Browser(stub_logging="off", real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content("<body></body>", url="https://fonts.test/"))
        # The evaluation does not wait for a promise, so the answer is left on the window.
        page.evaluate("window.__fonts = null; (" + ANSWER % json.dumps(list(PROBE_NAMES)) +
                      ").then(text => { window.__fonts = text; });")
        (await page.wait_for("window.__fonts !== null", timeout=5))
        ours = json.loads(page.evaluate("window.__fonts"))
    finally:
        (await browser.close())
    differences = []
    for name, answer in baseline.get("local", {}).items():
        mine = bool(ours.get(name))
        if mine != bool(answer.get("ok")):
            differences.append((name, bool(answer.get("ok")), mine))
    return differences


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read Chrome's answer about local fonts.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--visible", action="store_true",
                        help="run a visible Chrome, which a permission prompt needs")
    parser.add_argument("--compare", action="store_true",
                        help="diff this engine against the stored baseline instead")
    parser.add_argument("--timeout", type=float, default=60.0)
    arguments = parser.parse_args(argv)

    output = Path(arguments.output)
    if arguments.compare:
        if not output.exists():
            print(f"no baseline at {output}; run this tool without --compare first",
                  file=sys.stderr)
            return 1
        baseline = json.loads(output.read_text(encoding="utf-8"))
        differences = (run(compare(baseline)))
        if not differences:
            print(f"{len(baseline.get('local', {}))} names agree with {baseline['source']['browser']}")
            return 0
        for name, chrome, mine in differences:
            print(f"  {name}: Chrome {chrome}, ours {mine}")
        return 1

    baseline = collect(timeout=arguments.timeout, headless=not arguments.visible)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(baseline, indent=2) + "\n", encoding="utf-8")
    known = sum(1 for answer in baseline["local"].values() if answer.get("ok"))
    print(f"{len(baseline['local'])} names probed, {known} found, {output}")
    return 0


from pyv8c.async_api import Browser, run

if __name__ == "__main__":
    raise SystemExit(main())
