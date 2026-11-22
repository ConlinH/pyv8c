"""Regression coverage for animation, visibility and idle callbacks."""
from pyv8c.async_api import Browser
import pytest


@pytest.fixture
async def page():
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    try:
        yield browser.new_profile().new_page()
    finally:
        (await browser.close())


async def test_keyframes_numeric_duration_and_zero_rate(page):
    assert page.evaluate("""(() => {
      const a = document.body.animate([{opacity: 0}, {opacity: 1}], 100);
      a.playbackRate = 0;
      return [a.effect.getKeyframes().length, a.effect.getTiming().duration, a.playbackRate];
    })()""") == [2, 100, 0]


async def test_pause_delay_and_promise_identity(page):
    page.evaluate("""(() => {
      window.animationResult = [];
      const a = document.body.animate([{opacity: 0}, {opacity: 1}], {duration: 30, delay: 180});
      a.ready.then(value => animationResult.push(value === a));
      a.finished.then(value => animationResult.push(value === a));
      setTimeout(() => { a.pause(); animationResult.push(a.playState); }, 60);
      setTimeout(() => { animationResult.push(a.playState); a.play(); }, 120);
    })()""")
    (await page.wait_for('animationResult.length === 4', timeout=3))
    assert page.evaluate('animationResult') == [True, 'paused', 'paused', True]


async def test_cancel_rejects_and_completed_style_is_applied(page):
    page.evaluate("""(() => {
      window.cancelResult = 'pending';
      const e = document.createElement('div'); document.body.appendChild(e); e.id = 'anim-test';
      const a = e.animate([{opacity: '0'}, {opacity: '0.2'}], {duration: 30, fill: 'forwards'});
      a.finished.then(() => window.styleResult = getComputedStyle(e).opacity);
      const b = e.animate(null, 1000);
      b.finished.then(() => cancelResult = 'resolved', error => cancelResult = [error.name, error instanceof DOMException]);
      b.cancel();
    })()""")
    (await page.wait_for("window.cancelResult !== 'pending' && window.styleResult !== undefined", timeout=3))
    assert page.evaluate('[cancelResult, styleResult]') == [['AbortError', True], '0.2']


async def test_check_visibility_ancestor_and_optional_visibility(page):
    assert page.evaluate("""(() => {
      const p = document.createElement('div'), c = document.createElement('div');
      document.body.appendChild(p); p.appendChild(c); p.style.display = 'none';
      const hiddenAncestor = c.checkVisibility(); p.style.display = '';
      c.style.visibility = 'hidden';
      return [hiddenAncestor, c.checkVisibility(), c.checkVisibility({visibilityProperty: true})];
    })()""") == [False, True, False]


async def test_idle_timeout_reflects_time_spent_waiting(page):
    page.evaluate("""(() => {
      window.idleResult = null;
      requestIdleCallback(d => idleResult = [d.didTimeout, d.timeRemaining()], {timeout: 1});
      const start = Date.now(); while (Date.now() - start < 40) {}
    })()""")
    (await page.wait_for('idleResult !== null', timeout=3))
    assert page.evaluate('idleResult') == [True, 0]


async def test_timing_validation_is_atomic_and_effect_can_be_cleared(page):
    assert page.evaluate("""(() => {
      const results = [];
      for (const duration of [-1, 'bad']) {
        try { document.body.animate(null, {duration}); results.push('accepted'); }
        catch (error) { results.push(error.name); }
      }
      const a = document.body.animate(null, 100);
      try { a.effect.updateTiming({delay: 20, duration: 'bad'}); }
      catch (error) { results.push(error.name); }
      results.push(a.effect.getTiming().delay, a.effect.getTiming().duration);
      a.effect = null; a.finish(); results.push(a.effect === null, a.playState);
      return results;
    })()""") == ['TypeError', 'TypeError', 'TypeError', 0, 100, True, 'finished']


async def test_reverse_finished_animation_and_computed_timing(page):
    result = page.evaluate("""(() => {
      const a = document.body.animate(null, 1000); a.pause(); a.currentTime = 500;
      const timing = a.effect.getComputedTiming();
      const values = [timing.localTime, timing.progress, timing.currentIteration];
      a.finish(); a.reverse();
      return [values, a.playbackRate, a.currentTime > 900, a.playState];
    })()""")
    assert result == [[500, 0.5, 0], -1, True, 'running']


async def test_offsets_direction_and_geometry_follow_animation(page):
    assert page.evaluate("""(() => {
      const e = document.createElement('div'); e.style.width = '10px'; document.body.appendChild(e);
      const a = e.animate([{opacity: 0, width: '10px', offset: 0},
                           {opacity: 1, width: '100px', offset: 0.1},
                           {opacity: 0, width: '10px', offset: 1}],
                          {duration: 1000, fill: 'both'});
      a.pause(); a.currentTime = 100;
      const first = [getComputedStyle(e).opacity, getComputedStyle(e).width, e.getBoundingClientRect().width];
      const f = document.createElement('div'); document.body.appendChild(f);
      const r = f.animate([{opacity: 0}, {opacity: 1}], {duration: 1000, direction: 'reverse', fill: 'both'});
      r.pause(); r.currentTime = 250;
      return [first, getComputedStyle(f).opacity];
    })()""") == [['1', '100px', 100], '0.75']


async def test_finished_and_cancelled_animations_are_released(page):
    page.evaluate("""(() => {
      const e = document.createElement('div'); e.id = 'animation-cleanup'; document.body.appendChild(e);
      for (let i = 0; i < 10; i++) { const a = e.animate(null, 10); a.finished.catch(() => {}); a.cancel(); }
      for (let i = 0; i < 10; i++) e.animate(null, 10).finish();
    })()""")
    count = page.realm.executor.call(lambda: len(page.document.fn_getElementById('animation-cleanup')._animations))
    assert count == 0


async def test_hidden_can_be_overridden_by_author_css(page):
    assert page.evaluate("""(() => {
      const e = document.createElement('div'); e.hidden = true; document.body.appendChild(e);
      const before = e.checkVisibility(); e.style.display = 'block';
      return [before, e.checkVisibility()];
    })()""") == [False, True]


async def test_keyframe_validation_and_failed_update_do_not_break_layout(page):
    assert page.evaluate("""(() => {
      const e = document.createElement('div'); document.body.appendChild(e);
      let failures = [];
      for (const frames of [[{offset: 'bad'}, {offset: 1}], [{offset: .8}, {offset: .2}]]) {
        try { e.animate(frames, 100); failures.push('accepted'); }
        catch (error) { failures.push(error.name); }
      }
      const a = e.animate([{opacity: 0}, {opacity: 1}], 100); a.pause(); a.currentTime = 50;
      try { a.effect.setKeyframes([{offset: -1}]); } catch (error) { failures.push(error.name); }
      try { e.animate(null, {duration: 100, easing: 'bad-easing'}); }
      catch (error) { failures.push(error.name); }
      return [failures, a.effect.getKeyframes().length, getComputedStyle(e).opacity];
    })()""") == [['TypeError', 'TypeError', 'TypeError', 'TypeError'], 2, '0.5']


async def test_seek_after_finish_restores_effect_and_promise(page):
    result = page.evaluate("""(() => {
      const e = document.createElement('div'); document.body.appendChild(e);
      const a = e.animate([{opacity: 0}, {opacity: 1}], 1000);
      a.finish(); const old = a.finished; a.currentTime = 500;
      return [a.playState, a.finished !== old, Number(getComputedStyle(e).opacity)];
    })()""")
    assert result[:2] == ['running', True]
    assert 0.49 <= result[2] < 0.7


async def test_effect_transfer_and_null_current_time(page):
    assert page.evaluate("""(() => {
      const a = document.body.animate(null, 1000), b = document.body.animate(null, 1000);
      a.pause(); b.pause(); const effect = a.effect; b.effect = effect;
      const transferred = [a.effect === null, b.effect === effect];
      a.finished.catch(() => {}); a.cancel(); a.currentTime = null;
      let refusal; try { b.currentTime = null; refusal = 'accepted'; } catch (e) { refusal = e.name; }
      return [transferred, a.currentTime === null, refusal];
    })()""") == [[True, True], True, 'TypeError']


async def test_finish_event_and_zero_iterations(page):
    page.evaluate("""(() => {
      window.finishResult = [];
      const a = document.body.animate(null, {duration: Infinity, iterations: 0});
      const t = a.effect.getComputedTiming(); finishResult.push(t.activeDuration, t.endTime);
      a.addEventListener('finish', () => finishResult.push('event'));
      a.onfinish = () => finishResult.push('handler');
      a.finish(); finishResult.push('called');
    })()""")
    (await page.wait_for('finishResult.length === 5', timeout=3))
    assert page.evaluate('finishResult') == [0, 0, 'called', 'event', 'handler']


async def test_finished_fill_animation_allows_layout_cache(page):
    page.evaluate("""(() => {
      const e = document.createElement('div'); e.id = 'filled'; document.body.appendChild(e);
      e.animate([{width: '10px'}, {width: '100px'}], {duration: 100, fill: 'forwards'}).finish();
      e.getBoundingClientRect();
    })()""")
    def snapshot_reused():
        from pyv8c.web.dom.layout import layout_snapshot
        first = layout_snapshot(page.document)[0]
        second = layout_snapshot(page.document)[0]
        return first is second
    assert page.realm.executor.call(snapshot_reused)
    assert page.evaluate("document.getElementById('filled').getBoundingClientRect().width") == 100


async def test_overall_progress_tracks_effect_end_and_unresolved_time(page):
    result = page.evaluate('''(() => {
        const a = document.body.animate([], {duration: 500, iterations: 2, delay: 100});
        const result = [a.overallProgress];
        a.pause();
        for (const time of [-1, 0, 550, 1100, 1500]) {
            a.currentTime = time;
            result.push(a.overallProgress);
        }
        a.cancel(); result.push(a.overallProgress);
        const zero = document.body.animate([], 0);
        result.push(zero.overallProgress); zero.cancel();
        return result;
    })()''')
    from pyv8c import pyv8
    assert result == [0, 0, 0, 0.5, 1, 1, pyv8.Null, 1]
