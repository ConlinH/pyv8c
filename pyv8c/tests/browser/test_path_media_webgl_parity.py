"""Desktop graphics and media-query results compared with Chrome 157."""
import json
import sys

import pytest

from pyv8c.async_api import Browser


async def evaluate(script):
    browser = (await Browser(real_network=False, stub_logging="off").__aenter__())
    try:
        page = browser.new_profile().new_page()
        return json.loads(page.evaluate("JSON.stringify(" + script + ")"))
    finally:
        (await browser.close())


async def test_current_path_fill_rules_boundaries_and_creation_transform():
    assert (await evaluate("""(() => {
        const c=document.createElement('canvas').getContext('2d');
        c.rect(0,0,10,10);c.rect(2,2,6,6);
        const nested=[c.isPointInPath(5,5),c.isPointInPath(5,5,'evenodd'),
            c.isPointInPath(0,5),c.isPointInPath(10,10),
            c.isPointInPath(NaN,5),c.isPointInPath(Infinity,5)];
        c.beginPath();c.translate(10,20);c.rect(0,0,10,10);c.resetTransform();
        const transformed=[c.isPointInPath(15,25),c.isPointInPath(5,5)];
        c.fillStyle='red';c.fill();
        const pixel=Array.from(c.getImageData(15,25,1,1).data);
        c.beginPath();c.moveTo(0,0);c.lineTo(10,0);c.lineTo(0,10);
        const triangle=[c.isPointInPath(2,2),c.isPointInPath(9,9)];
        c.beginPath();c.rect(0,0,10,10);c.rect(8,2,-6,6);
        const opposite=c.isPointInPath(5,5);
        const errors=[];
        for(const f of [()=>c.isPointInPath(),()=>c.isPointInPath(1),
                       ()=>c.isPointInPath(1,2,'invalid')]) {
            try{f();errors.push('accepted')}catch(e){errors.push(e.name)}
        }
        return {nested,transformed,pixel,triangle,opposite,errors};
    })()""")) == {
        "nested": [True, False, True, True, False, False],
        "transformed": [True, False], "pixel": [255, 0, 0, 255],
        "triangle": [True, False], "opposite": False,
        "errors": ["TypeError", "TypeError", "TypeError"],
    }


async def test_monochrome_integer_ranges_on_a_color_display():
    assert (await evaluate("""[
        '(monochrome)','(monochrome:0)','(monochrome:1)',
        '(min-monochrome:0)','(max-monochrome:0)',
        '(max-monochrome:-1)','(monochrome:-1)','(monochrome:0.5)'
    ].map(q=>matchMedia(q).matches)""")) == [
        False, True, False, True, True, False, False, False,
    ]


async def test_anisotropy_limit_is_enabled_per_webgl_context():
    assert (await evaluate("""['webgl','webgl2'].map(kind=>{
        const gl=document.createElement('canvas').getContext(kind);
        gl.getSupportedExtensions();
        const before=[gl.getParameter(34047),gl.getError()];
        const ext=gl.getExtension('EXT_texture_filter_anisotropic');
        const after=[gl.getParameter(ext.MAX_TEXTURE_MAX_ANISOTROPY_EXT),gl.getError()];
        const other=document.createElement('canvas').getContext(kind);
        const isolated=[other.getParameter(34047),other.getError()];
        return {before,after,isolated,same:gl.getExtension('EXT_texture_filter_anisotropic')===ext};
    })""")) == [
        {"before": [None, 1280], "after": [16, 0], "isolated": [None, 1280], "same": True},
    ] * 2


@pytest.mark.parametrize('offscreen', [False, True])
async def test_path_coercion_order_defaults_and_exception_short_circuit(offscreen):
    canvas = "new OffscreenCanvas(32,32)" if offscreen else "document.createElement('canvas')"
    assert (await evaluate("""(() => {
        const c=CANVAS.getContext('2d');c.rect(0,0,10,10);
        const log=[],n=name=>({valueOf(){log.push(name);return 5}});
        const rule={toString(){log.push('rule');return 'nonzero'}};
        const inside=c.isPointInPath(n('x'),n('y'),rule);
        const defaultRule=c.isPointInPath('5','5',undefined);
        const before=log.length;
        try{c.isPointInPath({valueOf(){log.push('throw');throw new RangeError('stop')}},n('y'),rule)}
        catch(e){log.push(e.name)}
        return {inside,defaultRule,log,before};
    })()""".replace('CANVAS', canvas))) == {
        'inside': True, 'defaultRule': True, 'before': 3,
        'log': ['x', 'y', 'rule', 'throw', 'RangeError'],
    }


@pytest.mark.parametrize('offscreen', [False, True])
async def test_path_degeneracy_tiny_geometry_and_curved_boundary(offscreen):
    canvas = "new OffscreenCanvas(32,32)" if offscreen else "document.createElement('canvas')"
    assert (await evaluate("""(() => {
        const c=CANVAS.getContext('2d'),out=[];
        c.rect(0,0,0,10);out.push(c.isPointInPath(0,5));
        c.beginPath();c.moveTo(0,0);c.lineTo(10,10);out.push(c.isPointInPath(5,5));
        c.beginPath();c.moveTo(0,0);c.lineTo(0,0);c.lineTo(0,0);out.push(c.isPointInPath(0,0));
        c.beginPath();c.moveTo(0,0);c.lineTo(1e-7,0);c.lineTo(0,1e-7);
        out.push(c.isPointInPath(8e-8,8e-8),c.isPointInPath(2e-8,2e-8));
        c.beginPath();c.arc(0,0,1000,0,2*Math.PI);
        const angle=Math.PI/Math.floor(2*Math.PI*24);
        out.push(c.isPointInPath(999.95*Math.cos(angle),999.95*Math.sin(angle)),
                 c.isPointInPath(1000.05*Math.cos(angle),1000.05*Math.sin(angle)));
        c.beginPath();c.translate(20,30);c.scale(2,2);c.moveTo(0,0);
        c.bezierCurveTo(0,10,10,10,10,0);c.resetTransform();
        out.push(c.isPointInPath(30,36),c.isPointInPath(10,6));
        return out;
    })()""".replace('CANVAS', canvas))) == [
        False, False, False, False, True, True, False, True, False,
    ]


async def test_self_intersections_retracing_and_closed_path_continuation():
    assert (await evaluate("""(() => {
        const c=document.createElement('canvas').getContext('2d'),out=[];
        const hit=()=>[c.isPointInPath(0,0),c.isPointInPath(0,0,'evenodd')];
        c.moveTo(-10,-10);c.lineTo(10,10);c.lineTo(-10,10);c.lineTo(10,-10);out.push(hit());
        c.beginPath();c.moveTo(-10,0);c.bezierCurveTo(-10,-20,10,20,10,0);out.push(hit());
        c.beginPath();c.rect(-10,-10,20,20);c.rect(10,-10,-20,20);
        out.push(hit(),[c.isPointInPath(10,0),c.isPointInPath(10,0,'evenodd')]);
        c.beginPath();c.rect(0,0,10,10);c.lineTo(-10,0);c.lineTo(0,-10);out.push(hit());
        out.push([c.isPointInPath(5,5),c.isPointInPath(-2,-2)]);
        return out;
    })()""")) == [[True, False], [True, False], [False, False],
                [False, False], [False, False], [True, True]]


async def test_collapsed_path_endpoints_and_oppositely_traced_ellipse_boundaries():
    assert (await evaluate("""(() => {
        const c=document.createElement('canvas').getContext('2d'),out=[];
        const hit=(x,y)=>[c.isPointInPath(x,y),c.isPointInPath(x,y,'evenodd')];
        c.moveTo(0,-6);c.lineTo(0,6);out.push(hit(0,0),hit(0,-6),hit(0,6));
        c.beginPath();c.ellipse(0,0,0,6,0,0,2*Math.PI);
        out.push(hit(0,0),hit(0,3),hit(0,6));
        c.beginPath();c.ellipse(0,0,9,6,0,0,2*Math.PI);
        c.ellipse(0,0,9,6,0,0,2*Math.PI,true);
        out.push(hit(-9,0),hit(0,-6),hit(0,6));
        c.beginPath();c.scale(0,1);c.arc(0,0,10,0,2*Math.PI);c.resetTransform();
        out.push(hit(0,0),hit(0,10));
        return out;
    })()""")) == [[False, False], [True, True], [True, True],
                [True, True], [False, False], [True, True],
                [False, False], [False, False], [False, False],
                [False, False], [False, False]]


@pytest.mark.parametrize(('method', 'required'), [
    ('moveTo', 2), ('lineTo', 2), ('arc', 5), ('ellipse', 7),
    ('quadraticCurveTo', 4), ('bezierCurveTo', 6),
])
async def test_path_methods_require_and_convert_all_arguments(method, required):
    script = """(() => {
        const c=document.createElement('canvas').getContext('2d'),log=[],errors=[];
        try{c.METHOD()}catch(e){errors.push([e.name,e.message])}
        c.METHOD(...Array.from({length:REQUIRED},(_,i)=>({valueOf(){log.push(i);return NaN}})));
        return {log,errors};
    })()""".replace('METHOD', method).replace('REQUIRED', str(required))
    assert (await evaluate(script)) == {
        'log': list(range(required)),
        'errors': [['TypeError', f"Failed to execute '{method}' on 'CanvasRenderingContext2D': "
                    f'{required} arguments required, but only 0 present.']],
    }


@pytest.mark.parametrize(('value', 'expected'), [
    ('0', [True, True, True]), ('+0', [True, True, True]),
    ('-0', [True, True, True]), ('-1', [False, True, False]),
    ('0.0', [False, False, False]), ('1e0', [False, False, False]),
    ('\u0660', [False, False, False]),
    ('calc(0)', [True, True, True]), ('calc(-1)', [False, True, False]),
    ('calc(0.2)', [True, True, True]), ('calc(0.5)', [False, False, True]),
    ('calc(-0.5)', [True, True, True]), ('calc(-0.6)', [False, True, False]),
    ('calc(1 - 1)', [True, True, True]), ('calc(1-1)', [False, False, False]),
    ('calc(1 + -1)', [True, True, True]), ('calc(- 1)', [False, False, False]),
    ('calc(2 * 0)', [True, True, True]), ('calc(0 / 1)', [True, True, True]),
    ('calc(1 / 0)', [False, False, True]), ('calc(0 / 0)', [True, True, True]),
    ('calc(infinity)', [False, False, True]), ('calc(-infinity)', [False, True, False]),
    ('calc(NaN)', [True, True, True]), ('calc(1e0 - 1)', [True, True, True]),
    ('min(0, 1)', [True, True, True]), ('max(0, -1)', [True, True, True]),
    ('clamp(0, -1, 1)', [True, True, True]), ('calc(min(0, 1) + 0)', [True, True, True]),
    ('calc(0px)', [False, False, False]), ('calc(1px / 1px - 1)', [True, True, True]),
    ('calc(1/**/+/**/-1)', [False, False, False]),
    ('calc(1/**/ + /**/-1)', [True, True, True]), ('calc(0 + 0)junk', [False, False, False]),
])
async def test_monochrome_css_integer_math_observed_in_chrome(value, expected):
    queries = [f'({prefix}monochrome:{value})' for prefix in ('', 'min-', 'max-')]
    assert (await evaluate(json.dumps(queries) + '.map(q=>matchMedia(q).matches)')) == expected


async def test_long_media_integer_tokens_do_not_change_host_integer_limits():
    before = sys.get_int_max_str_digits()
    assert (await evaluate("""['9'.repeat(4500),'-'+'9'.repeat(4500),'0'.repeat(4500)]
        .map(value=>['','min-','max-'].map(prefix=>matchMedia('('+prefix+'monochrome:'+value+')').matches))""")) == [
        [False, False, True], [False, True, False], [True, True, True],
    ]
    assert sys.get_int_max_str_digits() == before


@pytest.mark.parametrize(('query', 'expected'), [
    ('', True), ('only screen', True), ('not print', True), ('not screen', False),
    ('(monochrome:calc(0)), (monochrome:1)', True),
    ('(monochrome:min(0,1)) and (monochrome:0)', True),
    ('(monochrome:0) or (monochrome:1)', True),
    ('((monochrome:0) and (monochrome:0))', True),
    ('not (monochrome)', True), ('not (monochrome:0)', False),
    ('not (unknown)', False), ('not (monochrome:invalid)', False),
    ('not (color:0.0)', False), ('not (color-index:invalid)', False),
    ('not (orientation:invalid)', False), ('not (hover:invalid)', False),
    ('not (prefers-color-scheme:invalid)', False),
    ('not (prefers-color-scheme:dark)', True), ('not (hover:none)', True),
    ('(min-monochrome:0) and (unknown)', False),
    ('(max-monochrome:-1) and (unknown)', False),
    ('not ((max-monochrome:-1) and (unknown))', True),
    ('(monochrome:0) and\t(monochrome:0)', True),
    ('(monochrome:0)/**/and/**/(monochrome:0)', True),
    ('(monochrome:0)garbage', False), ('(monochrome:0)(monochrome:1)', False),
    ('not screen and garbage', False), ('(monochrome:0)),(monochrome:0)', True),
])
async def test_balanced_media_query_conditions_and_error_recovery(query, expected):
    assert (await evaluate(f'matchMedia({json.dumps(query)}).matches')) is expected
