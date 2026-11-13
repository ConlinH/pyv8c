"""Embedding display:none removes child layout, as measured in Chrome 157."""
from pyv8c import Browser


def test_embedding_display_none_suppresses_and_restores_child_boxes():
    browser = Browser(real_network=False)
    try:
        page = browser.new_profile().new_page()
        page.set_content('<body><section><iframe></iframe></section></body>')
        assert page.evaluate("""(() => {
          const holder = document.querySelector('section');
          const frame = holder.firstElementChild;
          const child = frame.contentDocument;
          child.body.innerHTML = '<div style="width:100px;height:20px;padding:4px"></div>';
          const element = child.querySelector('div');
          return ['', 'display:none', 'visibility:hidden', 'opacity:0',
                  'content-visibility:hidden', ''].map(css => {
            holder.style.cssText = css;
            const rect = element.getBoundingClientRect();
            const style = frame.contentWindow.getComputedStyle(element);
            return [element.offsetWidth, element.offsetHeight,
              element.clientWidth, element.clientHeight,
              element.scrollWidth, element.scrollHeight,
              rect.width, rect.height, element.getClientRects().length,
              element.offsetParent === null, style.width, style.height,
              element.checkVisibility()];
          });
        })()""") == [
            [108, 28, 108, 28, 108, 28, 108, 28, 1, False, '100px', '20px', True],
            [0, 0, 0, 0, 0, 0, 0, 0, 0, True, '100px', '20px', False],
            *[[108, 28, 108, 28, 108, 28, 108, 28, 1, False, '100px', '20px', True]] * 4,
        ]
    finally:
        browser.close()


def test_nested_frame_layout_uses_current_outer_style():
    browser = Browser(real_network=False)
    try:
        page = browser.new_profile().new_page()
        page.set_content('<body><iframe></iframe></body>')
        assert page.evaluate("""(() => {
          const outer = document.querySelector('iframe');
          outer.contentDocument.body.innerHTML = '<iframe></iframe>';
          const inner = outer.contentDocument.querySelector('iframe');
          const child = inner.contentDocument;
          child.body.innerHTML = '<div style="width:100px">text</div>';
          const element = child.querySelector('div');
          const before = element.offsetWidth;
          outer.style.display = 'none';
          const hidden = [element.offsetWidth, element.getClientRects().length,
            inner.contentWindow.getComputedStyle(element).height,
            child.documentElement.clientWidth, child.documentElement.clientHeight];
          outer.style.display = '';
          return [before, hidden, element.offsetWidth];
        })()""") == [100, [0, 0, 'auto', 0, 0], 100]
    finally:
        browser.close()
