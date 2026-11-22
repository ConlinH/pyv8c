"""Legacy mouse layer coordinates and srcElement are observable during click dispatch."""
from pyv8c.async_api import Browser


async def test_mouse_layers_and_src_element_before_and_after_dispatch():
    browser = (await Browser(real_network=False).__aenter__())
    try:
        page = browser.new_profile().new_page()
        (await page.set_content('''<body><div id="layer" style="position:relative;left:30px;top:40px;
            width:100px;height:50px;border:5px solid"><span id="child">text</span></div></body>''', wait_until="commit"))
        assert page.evaluate('''(()=>{
            const e=new MouseEvent('click',{clientX:10,clientY:20});
            const before=[e.layerX,e.layerY,e.srcElement===null];let read;
            child.onclick=e=>read=[e.layerX,e.layerY,e.srcElement===child];
            child.dispatchEvent(e);const box=layer.getBoundingClientRect();
            return [before,read,[box.x,box.y],e.srcElement===child];
        })()''') == [[10,20,True],[-28,-28,True],[38,48],True]
    finally:
        (await browser.close())
