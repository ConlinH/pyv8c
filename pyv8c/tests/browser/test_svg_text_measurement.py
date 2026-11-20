"""SVG text uses the font shaper and preserves the native SVG geometry interfaces."""
import pytest
import sys

pytest.importorskip('pyv8c.pyv8')


@pytest.mark.skipif(sys.platform != 'win32', reason='Chrome Windows Arial metrics')
def test_svg_parser_text_metrics_match_chrome_on_windows_arial(page):
    page.set_content('''<svg width="600" height="400"><g>
      <text x="10" y="200" style="font:150px Arial">H</text></g></svg>''', url='https://text.test/')
    assert page.evaluate('''(() => {
      const t=document.querySelector('text'), rect=r=>[r.x,r.y,r.width,r.height];
      return [t instanceof SVGTextElement,t.namespaceURI,t.getComputedTextLength(),
              rect(t.getBBox()),rect(t.getExtentOfChar(0)),rect(t.parentNode.getBBox()),
              [t.getStartPositionOfChar(0).x,t.getEndPositionOfChar(0).x]];
    })()''') == [True, 'http://www.w3.org/2000/svg', 108.328125,
                [10,64,108.328125,168], [10,64,108.328125,168],
                [10,64,108.328125,168], [10,118.328125]]


def test_svg_char_extent_uses_a_local_face_when_the_profile_font_file_is_absent(page):
    """A Windows catalog family with no file here must still measure.

    The packaged profile names Segoe UI. macOS has the record and not the file,
    so shaping falls back to one local face. The extent is that face's box.
    """
    page.set_content(
        '<svg><text y="20" style="font: 16px \'Segoe UI\'">H</text></svg>',
        url='https://text.test/')
    extent = page.evaluate('''(() => {
      const rect = document.querySelector('text').getExtentOfChar(0);
      return [rect.width, rect.height];
    })()''')
    assert extent[0] > 0
    assert extent[1] > 0


def test_svg_text_invalid_character_throws_and_removed_text_has_no_layout(page):
    page.set_content('<svg><text style="font:20px Arial">Hi</text></svg>',url='https://text.test/')
    assert page.evaluate('''(() => {
      const t=document.querySelector('text'); let error;
      try{t.getExtentOfChar(2)}catch(e){error=e.name}
      const full=t.getComputedTextLength(), parts=t.getSubStringLength(0,1)+t.getSubStringLength(1,1);
      t.remove();return [error,full===parts,t.getComputedTextLength(),t.getNumberOfChars()];
    })()''') == ['IndexSizeError',True,0,0]


def test_font_shorthand_cascades_into_svg_and_html_longhands(page):
    page.set_content('''<style>.sample {font:italic 24px/30px Arial} .sample {font-size:30px}</style>
      <div class="sample">H</div><svg><text class="sample">H</text></svg>''',url='https://text.test/')
    assert page.evaluate('''[...document.querySelectorAll('.sample')].map(e=>{
      const s=getComputedStyle(e);return [s.fontSize,s.fontFamily,s.fontStyle,s.lineHeight];
    })''') == [['30px','Arial','italic','30px']]*2
