"""Frame focus and rendering checks measured against local Chrome 157."""
from pyv8c import Browser


def test_frame_visibility_and_focusing_child_inputs():
    browser = Browser(real_network=False)
    try:
        page = browser.new_profile().new_page()
        page.set_content('<body><button>top</button></body>')
        assert page.evaluate("""(() => {
          return ['', 'display:none', 'visibility:hidden', 'opacity:0',
                  'content-visibility:hidden'].map(css => {
            document.querySelector('button').focus();
            const frame = document.createElement('iframe');
            frame.style.cssText = css;
            document.body.appendChild(frame);
            const child = frame.contentDocument;
            child.body.innerHTML = '<input>';
            const before = [document.hasFocus(), child.hasFocus(),
              child.body.checkVisibility(),
              child.body.checkVisibility({visibilityProperty:true, opacityProperty:true})];
            child.querySelector('input').focus();
            const after = [document.hasFocus(), child.hasFocus(),
              document.activeElement.tagName, child.activeElement.tagName,
              child.querySelector('input').tabIndex];
            frame.remove();
            return {before, after};
          });
        })()""") == [
            {'before': [True, False, True, True], 'after': [True, True, 'IFRAME', 'INPUT', 0]},
            {'before': [True, False, False, False], 'after': [True, False, 'BUTTON', 'BODY', 0]},
            {'before': [True, False, True, True], 'after': [True, True, 'IFRAME', 'INPUT', 0]},
            {'before': [True, False, True, True], 'after': [True, True, 'IFRAME', 'INPUT', 0]},
            {'before': [True, False, True, True], 'after': [True, True, 'IFRAME', 'INPUT', 0]},
        ]
    finally:
        browser.close()


def test_focus_chain_tracks_nested_frames_and_parent_focus_changes():
    browser = Browser(real_network=False)
    try:
        page = browser.new_profile().new_page()
        page.set_content('<body><button>top</button><iframe></iframe></body>')
        assert page.evaluate("""(() => {
          const outer = document.querySelector('iframe');
          const child = outer.contentDocument;
          child.body.innerHTML = '<iframe></iframe>';
          const inner = child.querySelector('iframe');
          const nested = inner.contentDocument;
          nested.body.innerHTML = '<input>';
          nested.querySelector('input').focus();
          const focused = [document.hasFocus(), child.hasFocus(), nested.hasFocus(),
            document.activeElement === outer, child.activeElement === inner];
          document.querySelector('button').focus();
          const moved = [document.hasFocus(), child.hasFocus(), nested.hasFocus()];
          outer.style.display = 'none';
          const hidden = nested.body.checkVisibility();
          outer.style.display = '';
          return {focused, moved, hidden, visible:nested.body.checkVisibility()};
        })()""") == {'focused': [True, True, True, True, True],
                     'moved': [True, False, False], 'hidden': False, 'visible': True}
    finally:
        browser.close()
