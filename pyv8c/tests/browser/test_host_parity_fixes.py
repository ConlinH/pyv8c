"""Chrome-shaped host answers for the seven Kasada parity repairs.

The expected values are measured from a clean Chrome 157 run against a locally
fulfilled empty document (``phase9_chrome_shape.json`` in the kasada task
directory), not guessed.  All probes are offline: no site traffic, no payload
rewriting, no page-world hooks on a challenged page.
"""
import json

import pytest

pytest.importorskip("pyv8c.pyv8", reason="native pyv8 extension is not importable")

ORIGIN = "https://www.otto.de/"


def _values(shared_page, source):
    """JS returns JSON text; compare parsed Python values, not its spacing."""
    return json.loads(shared_page.evaluate(source))


@pytest.fixture(autouse=True)
async def _document(shared_page):
    (await shared_page.set_content(
        "<!doctype html><html><head></head><body></body></html>", url=ORIGIN, wait_until="commit"))
    return shared_page


async def test_canvas_line_cap_and_join_keep_state(shared_page):
    assert _values(shared_page, """(() => {
      const x = document.createElement('canvas').getContext('2d');
      const out = [x.lineCap, x.lineJoin];
      x.lineCap = 'round'; x.lineJoin = 'bevel';
      out.push(x.lineCap, x.lineJoin);
      x.lineCap = 'nonsense';            // Chrome ignores an invalid enum
      out.push(x.lineCap);
      x.save(); x.lineCap = 'square'; x.restore();
      out.push(x.lineCap);
      return JSON.stringify(out);
    })()""") == ["butt", "miter", "round", "bevel", "round", "round"]


async def test_check_visibility_answers_attached_and_hidden_elements(shared_page):
    assert _values(shared_page, """(() => {
      const visible = document.createElement('div');
      document.body.appendChild(visible);
      const hidden = document.createElement('div');
      hidden.style.display = 'none';
      document.body.appendChild(hidden);
      const detached = document.createElement('div');
      return JSON.stringify([visible.checkVisibility(),
                             hidden.checkVisibility(),
                             detached.checkVisibility()]);
    })()""") == [True, False, False]


async def test_animate_returns_a_pending_animation_with_a_keyframe_effect(shared_page):
    assert _values(shared_page, """(() => {
      const el = document.createElement('div');
      document.body.appendChild(el);
      const a = el.animate([{opacity: 0}, {opacity: 1}], {duration: 1000});
      return JSON.stringify([a.playState, a.currentTime, a.startTime,
                             a.playbackRate, a.pending, a.id, a.replaceState,
                             a.effect && a.effect.constructor && a.effect.constructor.name]);
    })()""") == ["running", 0, None, 1, True, "", "active", "KeyframeEffect"]


async def test_computed_style_reports_css_initial_values(shared_page):
    assert _values(shared_page, """(() => {
      const cs = getComputedStyle(document.body);
      return JSON.stringify([cs.direction, cs.opacity, cs.zoom]);
    })()""") == ["ltr", "1", "1"]


async def test_computed_background_color_initial_and_declared(shared_page):
    assert _values(shared_page, """(() => {
      const el = document.createElement('div');
      document.body.appendChild(el);
      const initial = getComputedStyle(el).backgroundColor;
      el.style.backgroundColor = 'red';
      const declared = getComputedStyle(el).getPropertyValue('background-color');
      el.remove();
      return JSON.stringify([initial, declared, getComputedStyle(el).backgroundColor]);
    })()""") == ['rgba(0, 0, 0, 0)', 'rgb(255, 0, 0)', '']


async def test_computed_defaults_do_not_override_explicit_styles(shared_page):
    assert _values(shared_page, """(() => {
      const e = document.createElement('div');
      document.body.appendChild(e);
      const s = getComputedStyle(e);
      const defaults = [s.maxWidth, s.maxHeight, s.zIndex, s.transform];
      e.style.cssText = 'max-width: 40px; max-height: 30px; z-index: 2; transform: scale(2)';
      const changed = getComputedStyle(e);
      return JSON.stringify([defaults, changed.maxWidth, changed.maxHeight, changed.zIndex,
                             changed.transform !== 'none', e.style.maxWidth]);
    })()""") == [['none', 'none', 'auto', 'none'], '40px', '30px', '2', True, '40px']


async def test_computed_additional_defaults_and_explicit_values(shared_page):
    assert _values(shared_page, """(() => {
      const e = document.createElement('div'); document.body.appendChild(e);
      const values = s => [s.clear, s.content, s.boxShadow, s.tableLayout, s.aspectRatio, s.verticalAlign];
      const initial = values(getComputedStyle(e));
      e.style.cssText = 'clear: both; table-layout: fixed; vertical-align: top';
      const s = getComputedStyle(e);
      return JSON.stringify([initial, s.clear, s.tableLayout, s.verticalAlign]);
    })()""") == [['none', 'normal', 'none', 'auto', 'auto', 'baseline'],
                   'both', 'fixed', 'top']


async def test_computed_box_and_background_defaults_preserve_declarations(shared_page):
    assert _values(shared_page, """(() => {
      const e = document.createElement('div'); document.body.appendChild(e);
      let s = getComputedStyle(e);
      const before = [s.marginTop, s.paddingLeft, s.backgroundImage, s.backgroundRepeat,
                      getComputedStyle(document.body).marginTop];
      e.style.cssText = 'margin: 3px 7px; padding-left: 9px; background-image: url("x.png")';
      s = getComputedStyle(e);
      return JSON.stringify([before, s.marginTop, s.marginLeft, s.paddingLeft, s.backgroundImage !== 'none']);
    })()""") == [['0px', '0px', 'none', 'repeat', '8px'], '3px', '7px', '9px', True]


async def test_computed_border_defaults_follow_current_color(shared_page):
    assert _values(shared_page, """(() => {
      const e = document.createElement('div'); e.style.color = 'red'; document.body.appendChild(e);
      const s = getComputedStyle(e);
      return JSON.stringify([s.borderTopStyle, s.borderTopWidth, s.borderTopColor,
                             s.top, s.float, s.animationDuration, s.flexDirection]);
    })()""") == ['none', '0px', 'rgb(255, 0, 0)', 'auto', 'none', '0s', 'row']


async def test_computed_inherited_defaults_and_parent_values(shared_page):
    assert _values(shared_page, """(() => {
      const p = document.createElement('div'), e = document.createElement('div');
      p.appendChild(e); document.body.appendChild(p);
      const read = () => { const s = getComputedStyle(e); return [s.borderCollapse, s.borderSpacing,
          s.textTransform, s.cursor, s.textShadow, s.listStyleType]; };
      const defaults = read();
      p.style.cssText = 'border-collapse: collapse; border-spacing: 4px; text-transform: uppercase; cursor: pointer; list-style-type: square';
      return JSON.stringify([defaults, read()]);
    })()""") == [['separate', '0px', 'none', 'auto', 'none', 'disc'],
                   ['collapse', '4px', 'uppercase', 'pointer', 'none', 'square']]


async def test_computed_outline_overflow_and_alignment_defaults(shared_page):
    assert _values(shared_page, """(() => {
      const e = document.createElement('div'); e.style.color = 'red'; document.body.appendChild(e);
      const s = getComputedStyle(e);
      return JSON.stringify([s.borderRadius, s.overflowX, s.overflowY, s.textDecorationLine,
        s.outlineWidth, s.outlineStyle, s.outlineColor, s.alignItems, s.justifyContent, s.gap, s.inset]);
    })()""") == ['0px', 'visible', 'visible', 'none', '3px', 'none', 'rgb(255, 0, 0)',
                   'normal', 'normal', 'normal', 'auto']


async def test_computed_min_sizes_follow_parent_layout(shared_page):
    assert _values(shared_page, """(() => {
      const p = document.createElement('div'), e = document.createElement('div');
      p.appendChild(e); document.body.appendChild(p);
      const read = () => [getComputedStyle(e).minWidth, getComputedStyle(e).minHeight];
      const block = read(); p.style.display = 'flex'; const flex = read();
      e.style.minWidth = '10px'; return JSON.stringify([block, flex, read()]);
    })()""") == [['0px', '0px'], ['auto', 'auto'], ['10px', 'auto']]


async def test_dynamics_compressor_params_have_web_audio_nominal_ranges(shared_page):
    assert _values(shared_page, """(() => {
      const ac = new AudioContext();
      const n = ac.createDynamicsCompressor();
      const out = [n.release.minValue, n.release.maxValue, n.release.value,
                   n.threshold.minValue, n.threshold.maxValue,
                   n.attack.maxValue, n.knee.maxValue, n.ratio.maxValue];
      ac.close();
      return JSON.stringify(out);
    })()""") == [0, 1, 0.25, -100, 0, 1, 40, 20]


async def test_request_idle_callback_runs_later_with_a_real_deadline(shared_page):
    shared_page.evaluate("""(() => {
      window.__p9_test_idle = null;
      window.__p9_test_id = requestIdleCallback(function (d) {
        window.__p9_test_idle = {
          didTimeout: d.didTimeout,
          remainingType: typeof d.timeRemaining,
          remaining: d.timeRemaining()
        };
      });
      return 'armed';
    })()""")
    (await shared_page.wait_for("window.__p9_test_idle !== null", timeout=5))
    result = _values(shared_page, "JSON.stringify(window.__p9_test_idle)")
    assert result["didTimeout"] is False
    assert result["remainingType"] == "function"
    assert isinstance(result["remaining"], (int, float))
    assert 0 <= result["remaining"] <= 50.0


async def test_is_uvpa_returns_a_promise_resolving_false(shared_page):
    shared_page.evaluate("""(() => {
      window.__p9_test_uvpa = null;
      const p = PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable();
      window.__p9_test_uvpa_is_promise = !!(p && typeof p.then === 'function');
      p.then(function (v) { window.__p9_test_uvpa = v; });
      return 'armed';
    })()""")
    (await shared_page.wait_for("window.__p9_test_uvpa !== null", timeout=5))
    assert shared_page.evaluate("window.__p9_test_uvpa") is False
    assert shared_page.evaluate("window.__p9_test_uvpa_is_promise") is True


async def test_animation_ready_and_finished_promises_settle(shared_page):
    """Chrome settles ready on the next frame and finished after the duration.

    The pre-phase-10 implementation created two pending Promises and never
    settled either, so ``a.finished.then(...)`` hung forever
    (phase10_promise_probe.json: TIMEOUT).
    """
    shared_page.evaluate("""(() => {
      const el = document.createElement('div');
      document.body.appendChild(el);
      window.__p10_ready = null;
      window.__p10_finished = null;
      const a = el.animate([{opacity: 0}, {opacity: 1}], {duration: 30});
      a.ready.then(function () { window.__p10_ready = true; });
      a.finished.then(function () { window.__p10_finished = a.playState; });
      return 'armed';
    })()""")
    (await shared_page.wait_for("window.__p10_ready !== null", timeout=5))
    (await shared_page.wait_for("window.__p10_finished !== null", timeout=5))
    assert shared_page.evaluate("window.__p10_ready") is True
    assert shared_page.evaluate("window.__p10_finished") == "finished"


async def test_animation_finish_and_cancel_never_leave_a_hanging_promise(shared_page):
    shared_page.evaluate("""(() => {
      const el = document.createElement('div');
      document.body.appendChild(el);
      window.__p10_manual = [];
      const a = el.animate([{opacity: 0}, {opacity: 1}], {duration: 100000});
      a.finished.then(function () { window.__p10_manual.push('finish'); });
      a.finish();
      const b = el.animate([{opacity: 0}, {opacity: 1}], {duration: 100000});
      b.finished.then(function () { window.__p10_manual.push('unexpected'); },
                      function (error) { window.__p10_manual.push(error.name); });
      b.cancel();
      return 'armed';
    })()""")
    (await shared_page.wait_for("window.__p10_manual.length === 2", timeout=5))
    assert _values(shared_page, "JSON.stringify(window.__p10_manual)") == [
        "finish", "AbortError"]


async def test_image_decode_returns_a_settled_promise(shared_page):
    shared_page.evaluate("""(() => {
      window.__p10_decode = null;
      const img = new Image();
      img.src = 'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7';
      const p = img.decode();
      window.__p10_decode_is_promise = !!(p && typeof p.then === 'function');
      p.then(function () { window.__p10_decode = true; },
             function (e) { window.__p10_decode = 'rejected'; });
      return 'armed';
    })()""")
    (await shared_page.wait_for("window.__p10_decode !== null", timeout=5))
    assert shared_page.evaluate("window.__p10_decode_is_promise") is True
    assert shared_page.evaluate("window.__p10_decode") is True


# -- Phase 11: IDL-declared members whose implementation class had no getter.
#    Expected values come from phase11_host_zero_probe.json /
#    phase11_extended_probe.json (clean Chrome 157, zero site traffic).

async def test_video_element_reports_chrome_defaults_and_reflects_writes(shared_page):
    assert _values(shared_page, """(() => {
      const v = document.createElement('video');
      const out = [v.videoWidth, v.videoHeight, v.width, v.height, v.poster,
                   v.playsInline, v.disablePictureInPicture];
      v.width = 320; v.height = -4; v.playsInline = true;
      v.disablePictureInPicture = true;
      out.push(v.width, v.height, v.playsInline, v.disablePictureInPicture,
               v.getAttribute('width'), v.getAttribute('height'));
      return JSON.stringify(out);
    })()""") == [0, 0, 0, 0, "", False, False,
                320, 0, True, True, "320", "0"]


async def test_image_reflected_attributes_report_chrome_defaults(shared_page):
    assert _values(shared_page, """(() => {
      const i = document.createElement('img');
      const out = [i.currentSrc, i.decoding, i.isMap, i.loading,
                   i.referrerPolicy, i.sizes, i.srcset, i.useMap, i.x, i.y];
      i.decoding = 'sync'; i.loading = 'lazy'; i.isMap = true;
      i.referrerPolicy = 'no-referrer'; i.sizes = '50vw';
      i.srcset = 'a.png'; i.useMap = '#m';
      out.push(i.decoding, i.loading, i.isMap, i.referrerPolicy,
               i.sizes, i.srcset, i.useMap);
      i.decoding = 'bogus';       // Chrome keeps the previous valid enum
      out.push(i.decoding);
      return JSON.stringify(out);
    })()""") == ["", "auto", False, "auto", "", "", "", "", 0, 0,
                "sync", "lazy", True, "no-referrer", "50vw", "a.png", "#m",
                "sync"]


async def test_canvas_2d_direction_quality_and_dash_keep_state(shared_page):
    assert _values(shared_page, """(() => {
      const x = document.createElement('canvas').getContext('2d');
      const out = [x.direction, x.imageSmoothingQuality, x.getLineDash().length];
      x.direction = 'rtl'; x.imageSmoothingQuality = 'high';
      x.setLineDash([4, 2]);
      out.push(x.direction, x.imageSmoothingQuality,
               x.getLineDash().length, x.getLineDash()[0]);
      x.save();
      x.direction = 'ltr'; x.imageSmoothingQuality = 'low'; x.setLineDash([9]);
      x.restore();
      out.push(x.direction, x.imageSmoothingQuality,
               x.getLineDash().length, x.getLineDash()[0]);
      x.direction = 'bogus';
      out.push(x.direction);
      return JSON.stringify(out);
    })()""") == ["inherit", "low", 0,
                "rtl", "high", 2, 4,
                "rtl", "high", 2, 4,
                "rtl"]
