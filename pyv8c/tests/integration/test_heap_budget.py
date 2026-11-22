"""A page that outgrows the isolate's heap budget is stopped, and the host lives."""

import asyncio
import pytest
import subprocess
import sys
import textwrap

#: The budget is a process-wide V8 setting, so this runs in a process of its own.
SCRIPT = '\nfrom pyv8c.async_api import Browser, run\nasync def _program():\n\n    from pyv8c import pyv8\n\n    pyv8.configure(params=["--max-old-space-size=256"])\n\n    from pyv8c.async_api import Browser, run\n    from pyv8c.web.fetch.response import Response\n\n    HTML = ("<body><script>var a = [];"\n            " for (let i = 0; i < 200000000; i++) a.push(i);</script></body>")\n\n\n    def loader(url, request):\n        return Response(HTML, {"url": url, "content-type": "text/html"})\n\n\n    browser = (await Browser(resource_loader=loader, stub_logging="off").__aenter__())\n    try:\n        page = browser.new_profile().new_page()\n        try:\n            await page.set_content(HTML, url="http://example.test/")\n        except pyv8.JavaScriptTerminated:\n            print("terminated")\n        else:\n            raise AssertionError("heap termination was swallowed by script execution")\n        print("readyState", page.evaluate("document.readyState"))\n        print("terminations", page.v8_context.heap_terminations)\n    finally:\n        (await browser.close())\n    print("closed")\nrun(_program())\n'


@pytest.mark.slow  # a V8 heap limit takes the process down, not just the test
async def test_a_page_that_outgrows_the_heap_is_stopped_and_the_host_survives():
    """The host receives heap termination and can still inspect and close the page."""
    result = (await asyncio.to_thread(subprocess.run,
        [sys.executable, "-c", textwrap.dedent(SCRIPT)],
        capture_output=True, text=True, timeout=120,
    ))
    assert result.returncode == 0, result.stderr
    tokens = result.stdout.split()
    assert tokens[0] == "terminated"
    assert tokens[1] == "readyState" and tokens[2] in ("loading", "interactive", "complete")
    assert tokens[3] == "terminations" and int(tokens[4]) >= 1
    assert tokens[5:] == ["closed"]
