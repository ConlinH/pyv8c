"""The PX captcha page under a hard timeout, so a stuck JS task cannot hang the run.

    python px3.py                  # quiet: no trace, no stub logging
    python px3.py --trace          # the old verbose run
    python px3.py --timeout 120    # seconds before the child is killed
    python px3.py --hold 3.0       # seconds the challenge button is held down

``run_until_idle`` queries runnable work; it does not execute or interrupt scripts.
JavaScript runs under the execution group's native operation budget. The process
timeout also bounds work that cannot be interrupted by the V8 watchdog.
"""

import asyncio
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from loguru import logger
from pyv8c.async_api import Browser

URL = ("https://www.skyscanner.com.au/sttc/px/captcha-v2/index.html?url=Lz8=&uuid=b74291f0-c061-11f1-ae58-2588bb3de0f3&vid=72d0bd15-c061-11f1-9900-dad3975d6")
LOG = Path(__file__).with_name("px3.log")

#: Where the challenge button's centre is, read from the page's own layout.
BUTTON = r"""
(function () {
  var node = document.querySelector('#px-captcha');
  if (!node) { return null; }
  var target = null, box = null, all = node.querySelectorAll('*');
  for (var i = 0; i < all.length; i++) {
    // The button is the smallest box that still carries the challenge's wording.
    var rect = all[i].getBoundingClientRect();
    if (rect.width < 8 || rect.height < 8) continue;
    if (!/press/i.test(all[i].textContent || '')) continue;
    if (box && rect.width >= box.width) continue;
    target = all[i]; box = rect;
  }
  if (!box) { box = node.getBoundingClientRect(); }
  if (!box || box.height < 4) { return null; }
  return { x: box.x + box.width / 2, y: box.y + box.height / 2,
           w: box.width, h: box.height,
           text: String((target || node).textContent || '').replace(/\s+/g, ' ').trim().slice(0, 40) };
})()
"""

#: The container appears first, then a loader: the wording is what says it is ready.
READY = r"""
(function () {
  var node = document.querySelector('#px-captcha');
  return !!node && node.getBoundingClientRect().height > 4
      && /press/i.test(node.textContent || '');
})()
"""

RESULT = r"""
(function () {
  var node = document.querySelector('#px-captcha');
  return { url: location.href.slice(0, 90),
           widget: node ? Math.round(node.getBoundingClientRect().width) : 0,
           text: String(document.body.innerText || '').replace(/\s+/g, ' ').slice(0, 120) };
})()
"""


async def pump(page, seconds=0.0):
    """Let the queue turn for *seconds*.

    A running challenge keeps its own timers going, so the page need not reach
    idle; a realm that is busy mid-task is simply left to finish and retried.
    """
    deadline = time.time() + seconds
    while True:
        try:
            (await page.run_until_idle(timeout=0))
        except Exception:  # noqa: BLE001
            pass
        if time.time() >= deadline:
            return
        (await asyncio.sleep(0.2))


async def wait_for_widget(page, seconds):
    """Poll until the challenge button is pressable, letting the page's timers run."""
    waited = 0.0
    while waited < seconds:
        if page.evaluate(READY):
            return True
        (await pump(page))
        (await asyncio.sleep(0.5))
        waited += 0.5
    return False


async def hold(page, seconds):
    """Move to the challenge button, press, hold, release, then let the page react."""
    if not (await wait_for_widget(page, 20)):
        print("no challenge widget appeared")
        return False
    spot = page.evaluate(BUTTON)
    if not spot:
        print("no challenge widget on the page")
        return False
    print(f"button {spot['text']!r} at ({spot['x']:.0f}, {spot['y']:.0f}) "
          f"{spot['w']:.0f}x{spot['h']:.0f}")
    (await page.mouse.move(spot["x"] - 150, spot["y"] + 60, steps=15))
    (await page.mouse.move(spot["x"], spot["y"], steps=10))
    (await page.mouse.down())
    step = 0.25
    waited = 0.0
    while waited < seconds:
        # Stay on the button while held, the way a hand does, and let the page run.
        (await page.mouse.move(spot["x"] + (1 if int(waited * 4) % 2 else -1), spot["y"], steps=1))
        (await asyncio.sleep(step))
        (await pump(page))
        waited += step
    (await page.mouse.up())
    print(f"released after {waited:.2f}s")
    return True


async def browse(stub_logging, trace, log, seconds):
    logger.add(log, level="DEBUG" if trace else "INFO", mode="w")
    started = time.time()
    browser = (await Browser(stub_logging=stub_logging, trace=trace, traceable=trace,
                      threaded_loaders=True).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.goto(URL))
        (await pump(page, 20))
        (await hold(page, seconds))
        (await pump(page, 20))
        try:
            print("after hold:", json.dumps(page.evaluate(RESULT)))
        except Exception as error:  # noqa: BLE001
            print("after hold: realm stayed busy:", type(error).__name__)
    finally:
        (await browser.close())
    print(f"browsed in {time.time() - started:.1f}s")


async def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--trace", action="store_true", help="verbose stubs and engine trace")
    parser.add_argument("--timeout", type=float, default=300.0, help="seconds before the child is killed")
    parser.add_argument("--hold", type=float, default=2.5, help="seconds to hold the challenge button")
    parser.add_argument("--log", type=Path, default=LOG, help="where the child logs")
    arguments = parser.parse_args(argv)

    if arguments.child:
        (await browse("verbose" if arguments.trace else "off", arguments.trace, arguments.log,
               arguments.hold))
        return 0

    command = [sys.executable, "-u", __file__, "--child", "--log", str(arguments.log),
               "--hold", str(arguments.hold)]
    if arguments.trace:
        command.append("--trace")
    started = time.time()
    process = subprocess.Popen(command)
    try:
        code = (await asyncio.to_thread(process.wait, timeout=arguments.timeout))
    except subprocess.TimeoutExpired:
        process.kill()
        (await asyncio.to_thread(process.wait, timeout=10))
        print(f"killed after {arguments.timeout:.0f}s: the page never ran out of work", file=sys.stderr)
        return 1
    print(f"child exited {code} after {time.time() - started:.1f}s")
    return code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
