"""SVG text uses the font shaper and preserves the native SVG geometry interfaces."""
import pytest
import sys

pytest.importorskip('pyv8c.pyv8')


def test_font_shorthand_cascades_into_svg_and_html_longhands(page):
    page.set_content('''<style>.sample {font:italic 24px/30px Arial} .sample {font-size:30px}</style>
      <div class="sample">H</div><svg><text class="sample">H</text></svg>''',url='https://text.test/')
    assert page.evaluate('''[...document.querySelectorAll('.sample')].map(e=>{
      const s=getComputedStyle(e);return [s.fontSize,s.fontFamily,s.fontStyle,s.lineHeight];
    })''') == [['30px','Arial','italic','30px']]*2
