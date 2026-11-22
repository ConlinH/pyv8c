"""The members a case run reported as 待实现, with the behaviour the browser gives them."""
import json

from loguru import logger

from pyv8c.async_api import Browser


async def _page(browser):
    page = browser.new_profile().new_page()
    (await page.set_content(
        "<body><textarea id=t></textarea><option id=o>one</option><div id=d></div>"
        "<canvas id=c></canvas><canvas id=c2></canvas></body>", url="https://repro.test/index.html", wait_until="commit"))
    (await page.run_until_idle(timeout=2))
    return page


async def test_the_reported_members_answer_like_chromium():
    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        page = (await _page(browser))
        seen = json.loads(page.evaluate("""JSON.stringify((() => {
            const area = document.getElementById('t');
            const option = document.getElementById('o');
            const div = document.getElementById('d');
            const context = new OffscreenCanvas(2, 2).getContext('2d');
            const maxLengthInitial = area.maxLength;
            const negative = (() => {
                try { area.maxLength = -1; return 'no throw'; }
                catch (error) { return error.name + ': ' + error.message; }
            })();
            area.maxLength = 10;
            const maxLengthSet = [area.maxLength, area.getAttribute('maxlength')];
            area.dirName = 'rtl';
            option.defaultSelected = true;
            const optionSet = [option.defaultSelected, option.getAttribute('selected'),
                               option.selected];
            option.defaultSelected = false;
            div.style = 'color: red';
            const styleSet = [div.style.getPropertyValue('color'), div.getAttribute('style')];
            div.style = null;
            const host = document.createElement('div');
            document.body.appendChild(host);
            const root = host.attachShadow({mode: 'open'});
            root.innerHTML = '<button id="inner">b</button>';
            const before = root.activeElement === null;
            const inner = root.querySelector('button');
            inner.focus();
            const inside = root.activeElement === inner;
            area.focus();
            const outside = root.activeElement === null;
            const gl = document.getElementById('c').getContext('webgl2');
            const samples = gl.getInternalformatParameter(gl.RENDERBUFFER, gl.RGBA8, gl.SAMPLES);
            gl.getError();
            const bad = [String(gl.getInternalformatParameter(gl.TEXTURE_2D, gl.RGBA8, gl.SAMPLES)),
                         String(gl.getInternalformatParameter(gl.RENDERBUFFER, gl.RGBA32I, gl.SAMPLES)),
                         String(gl.getInternalformatParameter(gl.RENDERBUFFER, gl.RGBA8, 0)),
                         gl.getError()];
            return {
                pixelFormat: context.getImageData(0, 0, 1, 1).pixelFormat,
                maxLengthInitial: maxLengthInitial, maxLengthNegative: negative,
                maxLengthSet: maxLengthSet, dirName: [area.dirName, area.getAttribute('dirname')],
                optionSet: optionSet, optionCleared: option.defaultSelected,
                styleSet: styleSet, styleNull: div.style.cssText,
                shadow: [before, inside, outside],
                samples: [samples.constructor.name, Array.from(samples)], bad: bad,
            };
        })())"""))
    finally:
        (await browser.close())

    assert seen == {
        "pixelFormat": "rgba-unorm8",
        "maxLengthInitial": -1,
        "maxLengthNegative": "IndexSizeError: Failed to set the 'maxLength' property on"
                             " 'HTMLTextAreaElement': The provided value (-1) is not positive or 0.",
        "maxLengthSet": [10, "10"],
        "dirName": ["rtl", "rtl"],
        "optionSet": [True, "", True],
        "optionCleared": False,
        "styleSet": ["red", "color: red;"],
        "styleNull": "",
        "shadow": [True, True, True],
        "samples": ["Int32Array", [4]],
        "bad": ["null", "null", "null", 1280],
    }


async def test_no_reported_member_reports_itself_as_unimplemented():
    messages = []
    token = logger.add(lambda message: messages.append(str(message)), format="{message}")
    try:
        browser = (await Browser(stub_logging="on").__aenter__())
        try:
            page = (await _page(browser))
            page.evaluate("""(() => {
                const area = document.getElementById('t');
                area.maxLength = 10; area.dirName = 'rtl';
                document.getElementById('o').defaultSelected = true;
                document.getElementById('d').style = 'color: red';
                const context = new OffscreenCanvas(2, 2).getContext('2d');
                context.getImageData(0, 0, 1, 1).pixelFormat;
                const host = document.createElement('div');
                document.body.appendChild(host);
                const root = host.attachShadow({mode: 'open'});
                root.activeElement;
                const gl = document.getElementById('c').getContext('webgl2');
                gl.getInternalformatParameter(gl.RENDERBUFFER, gl.RGBA8, gl.SAMPLES);
                const surface = document.getElementById('c2').getContext('2d');
                surface.shadowColor = 'red';
                surface.bezierCurveTo(1, 1, 2, 2, 3, 3);
                surface.quadraticCurveTo(4, 4, 5, 5);
                surface.ellipse(0, 0, 1, 1, 0, 0, 1);
                document.importNode(document.getElementById('d'), true);
                navigator.cpuPerformance;
                navigator.getGamepads();
                createImageBitmap(document.getElementById('c2'));
                Summarizer.availability();
            })()""")
        finally:
            (await browser.close())
    finally:
        logger.remove(token)

    named = ("maxLength", "dirName", "defaultSelected", "pixelFormat", "style", "activeElement",
             "getInternalformatParameter", "shadowColor", "bezierCurveTo", "quadraticCurveTo",
             "ellipse", "importNode", "cpuPerformance", "getGamepads", "createImageBitmap",
             "availability")
    assert [message for message in messages
            if "待实现" in message and any(name in message for name in named)] == []


async def test_the_canvas_and_navigator_reports_answer_like_chromium():
    """The second batch: the canvas path/colour members, importNode, gamepads and the AI API."""
    browser = (await Browser(stub_logging="off").__aenter__())
    try:
        page = (await _page(browser))
        seen = json.loads(page.evaluate("""JSON.stringify((() => {
            const attempt = (action) => {
                try { return 'ok:' + String(action()); }
                catch (error) { return error.name + ': ' + error.message; }
            };
            const canvas = document.getElementById('c2');
            const context = canvas.getContext('2d');
            const out = {};
            out.shadowDefault = context.shadowColor;
            context.shadowColor = 'red';
            out.shadowRed = context.shadowColor;
            context.shadowColor = 'not a color';
            out.shadowInvalid = context.shadowColor;
            context.shadowColor = 'rgba(1, 2, 3, 0.5)';
            out.shadowRgba = context.shadowColor;
            out.paths = attempt(() => {
                context.beginPath(); context.moveTo(0, 0);
                context.bezierCurveTo(1, 1, 2, 2, 3, 3); context.quadraticCurveTo(4, 4, 5, 5);
                return 'drew';
            });
            out.ellipseNegative = attempt(() => {
                context.ellipse(0, 0, -1, 1, 0, 0, 1); return 'no throw';
            });
            out.ellipseNonFinite = attempt(() => {
                context.ellipse(0, 0, Infinity, 1, 0, 0, 1); return 'ignored';
            });
            out.imported = attempt(() => {
                const div = document.createElement('div');
                div.innerHTML = '<b>x</b>';
                return document.importNode(div, true).outerHTML;
            });
            out.importDocument = attempt(() => String(document.importNode(document, true)));
            out.importNotNode = attempt(() => String(document.importNode({}, true)));
            out.cpuPerformanceInRange =
                navigator.cpuPerformance >= 0 && navigator.cpuPerformance <= 4;
            out.gamepads = JSON.stringify(navigator.getGamepads());
            canvas.width = 8; canvas.height = 8;
            out.cropZeroWidth = attempt(() => String(createImageBitmap(canvas, 0, 0, 0, 4)));
            out.cropZeroHeight = attempt(() => String(createImageBitmap(canvas, 0, 0, 4, 0)));
            createImageBitmap(canvas).then((bitmap) => {
                window.__bitmap = [bitmap.width, bitmap.height];
                bitmap.close();
                window.__closed = [bitmap.width, bitmap.height];
            });
            Summarizer.availability().then((value) => { window.__availability = value; });
            return out;
        })())"""))
        (await page.run_until_idle(timeout=2))
        seen["bitmap"] = json.loads(page.evaluate("JSON.stringify(window.__bitmap)"))
        seen["closed"] = json.loads(page.evaluate("JSON.stringify(window.__closed)"))
        seen["availability"] = page.evaluate("String(window.__availability)")
    finally:
        (await browser.close())

    assert seen == {
        "shadowDefault": "rgba(0, 0, 0, 0)",
        "shadowRed": "#ff0000",
        "shadowInvalid": "#ff0000",
        "shadowRgba": "rgba(1, 2, 3, 0.5)",
        "paths": "ok:drew",
        "ellipseNegative": "IndexSizeError: Failed to execute 'ellipse' on"
                           " 'CanvasRenderingContext2D': The major-axis radius provided (-1)"
                           " is negative.",
        "ellipseNonFinite": "ok:ignored",
        "imported": "ok:<div><b>x</b></div>",
        "importDocument": "NotSupportedError: Failed to execute 'importNode' on 'Document':"
                          " The node provided is a document, which may not be imported.",
        "importNotNode": "TypeError: Failed to execute 'importNode' on 'Document':"
                         " parameter 1 is not of type 'Node'.",
        "cpuPerformanceInRange": True,
        "gamepads": "[null,null,null,null]",
        "cropZeroWidth": "RangeError: Failed to execute 'createImageBitmap' on 'Window':"
                         " The crop rect width is 0.",
        "cropZeroHeight": "RangeError: Failed to execute 'createImageBitmap' on 'Window':"
                          " The crop rect height is 0.",
        "bitmap": [8, 8],
        "closed": [0, 0],
        "availability": "unavailable",
    }
