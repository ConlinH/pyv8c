"""Offline audio must complete both its legacy event and its promise consumer."""
import pytest


async def test_completion_uses_media_source_and_remains_document_bound(page, monkeypatch):
    from pyv8c.foundation.tasks import TaskQueue, TaskSource
    posted = []
    original = TaskQueue.post_task

    def record(self, callback, source=TaskSource.DOM, **kwargs):
        if kwargs.get('label') == 'offline-audio-complete':
            posted.append((source, kwargs.get('document_bound')))
        return original(self, callback, source, **kwargs)

    monkeypatch.setattr(TaskQueue, 'post_task', record)
    page.evaluate('globalThis.done = false; new OfflineAudioContext(1,128,44100)'
                  '.startRendering().then(() => done = true)')
    (await page.wait_for('done', timeout=2))
    assert posted == [(TaskSource.MEDIA_ELEMENT, True)]


async def test_complete_event_precedes_promise_and_shares_the_rendered_buffer(page):
    page.evaluate("""
        globalThis.completion = null;
        globalThis.audioOrder = [];
        const context = new OfflineAudioContext(1, 128, 44100);
        let eventBuffer;
        context.addEventListener('complete', event => {
            audioOrder.push('listener');
            eventBuffer = event.renderedBuffer;
            globalThis.eventChecks = [event instanceof OfflineAudioCompletionEvent,
                event.renderedBuffer instanceof AudioBuffer, event.isTrusted,
                event.target === context, event.currentTarget === context,
                event.type, event.bubbles, event.cancelable];
        });
        context.oncomplete = () => audioOrder.push('old handler');
        context.startRendering().then(buffer => {
            audioOrder.push('promise');
            completion = [audioOrder.slice(), eventChecks, buffer === eventBuffer,
                          buffer.length, context.state];
        });
        // Completion is a queued task, so this replacement must take effect.
        context.oncomplete = () => audioOrder.push('handler');
        audioOrder.push('after-call');
    """)
    (await page.wait_for('completion !== null', timeout=2))
    assert page.evaluate('completion') == [
        ['after-call', 'listener', 'handler', 'promise'],
        [True, True, True, True, True, 'complete', True, False],
        True, 128, 'closed',
    ]


async def test_completion_event_constructor_keeps_buffer_identity_and_cannot_forge_trust(page):
    assert page.evaluate("""(() => {
        const buffer = new AudioBuffer({length: 8, sampleRate: 44100});
        const event = new OfflineAudioCompletionEvent('complete',
            {renderedBuffer: buffer, isTrusted: true});
        let readonly = false;
        try { (function () {'use strict'; event.renderedBuffer = null;})() }
        catch (error) { readonly = error instanceof TypeError; }
        return [event.renderedBuffer === buffer, event instanceof Event,
                event.isTrusted, event.bubbles, event.cancelable, readonly];
    })()""") == [True, True, False, False, False, True]


@pytest.mark.parametrize('arguments', [
    '', "'complete'", "'complete', null", "'complete', {}",
    "'complete', {renderedBuffer: null}", "'complete', {renderedBuffer: {}}",
])
async def test_completion_event_rejects_missing_or_invalid_buffer(page, arguments):
    assert page.evaluate("""(() => {
        try { new OfflineAudioCompletionEvent(%s); return false; }
        catch (error) { return error instanceof TypeError; }
    })()""" % arguments)
