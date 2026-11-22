"""MathML foreign content in HTML document and fragment parsing."""

from pyv8c.async_api import Browser


async def test_mathml_namespaces_and_color_hints_follow_the_html_parser():
    browser = (await Browser(real_network=False, stub_logging='off').__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('''<body><math><mrow id="row" mathcolor="blue"><mn>1</mn></mrow>
          <mi><span id="embedded">x</span></mi></math></body>''', url='https://math.test/', wait_until="commit"))
        assert page.evaluate('''(() => {
          const row = document.getElementById('row'), number = row.firstElementChild;
          const embedded = document.getElementById('embedded');
          const before = [row.namespaceURI, number.namespaceURI, embedded.namespaceURI,
                          getComputedStyle(row).color, getComputedStyle(number).color];
          row.style.color = 'red';
          const override = getComputedStyle(row).color;
          row.style.removeProperty('color'); row.setAttribute('mathcolor', 'green');
          const changed = getComputedStyle(row).color;
          const host = document.createElement('div');
          host.innerHTML = '<math><mrow mathcolor="blue"><mn>1</mn></mrow></math>';
          document.body.appendChild(host);
          const parsed = host.querySelector('mrow');
          const fragment = [parsed.namespaceURI, getComputedStyle(parsed).color];
          const htmlMath = document.createElement('math');
          htmlMath.innerHTML = '<mrow></mrow>';
          return {before, override, changed, fragment, htmlNamespace: htmlMath.firstElementChild.namespaceURI};
        })()''') == {
            'before': ['http://www.w3.org/1998/Math/MathML', 'http://www.w3.org/1998/Math/MathML',
                       'http://www.w3.org/1999/xhtml', 'rgb(0, 0, 255)', 'rgb(0, 0, 255)'],
            'override': 'rgb(255, 0, 0)', 'changed': 'rgb(0, 128, 0)',
            'fragment': ['http://www.w3.org/1998/Math/MathML', 'rgb(0, 0, 255)'],
            'htmlNamespace': 'http://www.w3.org/1999/xhtml',
        }
    finally:
        (await browser.close())
