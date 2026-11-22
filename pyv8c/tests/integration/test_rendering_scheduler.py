
import asyncio
import threading

from pyv8c.async_api import Browser


async def test_rendering_opportunity_is_not_a_timer_task():
    browser = (await Browser(real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        done = threading.Event()
        observed = []
        def frame(stamp):
            observed.append((page.realm.agent.tasks._current, len(page.tasks.timers)))
            done.set()
        page.executor.call(page.services.task_services.request_animation_frame, frame)
        assert (await asyncio.to_thread(done.wait, 3))
        assert observed == [(None, 0)]
    finally:
        (await browser.close())


async def test_hidden_tree_pauses_frames_but_keeps_other_tasks_running():
    browser = (await Browser(real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body><iframe></iframe></body>', wait_until="commit"))
        (await page.wait_for_load_state(timeout=3))
        page.set_visibility(True)
        page.evaluate("""globalThis.log=[];globalThis.timer=false;
            requestAnimationFrame(()=>log.push('parent'));
            frames[0].requestAnimationFrame(()=>log.push('child'));
            setTimeout(()=>timer=true,50);
        """)
        (await page.wait_for('timer', timeout=3))
        assert page.evaluate('log') == []
        assert page.executor.call(page.execution_group.rendering.next_deadline) is None
        assert (await page.run_until_idle(timeout=0))
        page.set_visibility(False)
        (await page.wait_for('log.length===2', timeout=3))
        assert page.evaluate('log') == ['parent', 'child']
        assert page.executor.call(page.execution_group.rendering.next_deadline) is None
    finally:
        (await browser.close())


async def test_navigation_and_close_release_frame_callbacks():
    browser = (await Browser(real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        page.set_visibility(True)
        page.evaluate('globalThis.oldRan=false;requestAnimationFrame(()=>oldRan=true)')
        old = page.executor.call(lambda: page.services.task_services._frames)
        (await page.set_content('<body>replacement</body>', wait_until="commit"))
        assert old.closed and not old.callbacks
        (await page.wait_for_load_state(timeout=3))
        page.evaluate('globalThis.done=false;requestAnimationFrame(()=>done=true)')
        (await page.wait_for('done', timeout=3))
        group = page.execution_group
        (await page.close())
        assert group.rendering.closed and not group.rendering._lanes
    finally:
        (await browser.close())


async def test_reinsert_does_not_resume_the_retired_child_frame():
    browser = (await Browser(real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('<body><iframe></iframe></body>', wait_until="commit"))
        (await page.wait_for_load_state(timeout=3))
        page.evaluate("""globalThis.log=[];globalThis.done=false;
            const frame=document.querySelector('iframe'),old=frame.contentWindow;
            old.requestAnimationFrame(()=>log.push('stale'));
            requestAnimationFrame(()=>{
                frame.remove();document.body.appendChild(frame);
                frame.contentWindow.requestAnimationFrame(()=>log.push('current'));
                requestAnimationFrame(()=>requestAnimationFrame(()=>done=true));
            });
        """)
        (await page.wait_for('done', timeout=3))
        assert page.evaluate('log') == ['current']
    finally:
        (await browser.close())
