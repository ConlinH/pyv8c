"""Opt-in native ANGLE smoke test: supply PYV8C_ANGLE_DIR with both libraries."""
import ctypes as c
import os
import struct
import sys
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from pyv8c.web.graphics.webgl.backends.egl import AngleContext, AngleError, bind_function
from pyv8c.web.graphics.webgl.backends import egl as angle_module


async def test_missing_libraries_fail_clearly(tmp_path):
    with pytest.raises(AngleError, match='ANGLE library not found'):
        AngleContext(tmp_path)


async def test_linux_prefers_surfaceless_display(monkeypatch):
    monkeypatch.setattr(angle_module, 'sys', SimpleNamespace(platform='linux'))
    context = AngleContext.__new__(AngleContext)
    context._query_string = lambda display, name: b'EGL_MESA_platform_surfaceless EGL_EXT_platform_base'
    callback_type = c.CFUNCTYPE(c.c_void_p, c.c_uint, c.c_void_p, c.POINTER(c.c_int32))
    callback = callback_type(lambda platform, native, attrs: 42 if platform == 0x31DD else None)
    context._get_proc_address = lambda name: c.cast(callback, c.c_void_p).value
    context._get_display = lambda display: pytest.fail('default display should not be used')
    assert context._select_display() == 42


async def test_linux_falls_back_when_surfaceless_is_unavailable(monkeypatch):
    monkeypatch.setattr(angle_module, 'sys', SimpleNamespace(platform='linux'))
    context = AngleContext.__new__(AngleContext)
    context._query_string = lambda display, name: b'EGL_EXT_platform_base'
    context._get_display = lambda display: 73
    assert context._select_display() == 73


async def test_browser_reports_missing_angle_libraries(tmp_path):
    from pyv8c.async_api import Browser
    with pytest.raises(AngleError, match='ANGLE library not found'):
        (await Browser(real_network=False, angle_library_dir=tmp_path).__aenter__())


async def test_browser_reports_missing_optional_dependency(monkeypatch):
    from pyv8c.async_api import Browser
    monkeypatch.setitem(sys.modules, 'comfy_angle', None)
    with pytest.raises(ImportError, match=r'install pyv8c\[angle\]'):
        (await Browser(real_network=False, webgl_backend='angle').__aenter__())


async def test_default_browser_does_not_import_angle(monkeypatch):
    from pyv8c.async_api import Browser
    monkeypatch.setitem(sys.modules, 'comfy_angle', None)
    browser = (await Browser(real_network=False).__aenter__())
    try:
        assert browser._graphics_backend is None
    finally:
        (await browser.close())


async def test_browser_rejects_unknown_webgl_backend():
    from pyv8c.async_api import Browser
    with pytest.raises(ValueError, match='webgl_backend'):
        (await Browser(real_network=False, webgl_backend='invalid').__aenter__())


async def test_webgl_buffer_hook_receives_the_typed_array_view():
    from pyv8c.async_api import Browser

    seen = []

    def capture(receiver, target, data, usage):
        values = [data[index] for index in range(int(data._get('length')))]
        seen.append(struct.pack('<' + 'f' * len(values), *values))

    browser = (await Browser(real_network=False, interface_overrides={
        'WebGLRenderingContext': {'bufferData': capture}}).__aenter__())
    try:
        page = browser.new_profile().new_page()
        page.evaluate('''(() => {
          const gl = document.createElement('canvas').getContext('webgl');
          const array = new Float32Array([99, -1, 0.5, 88]);
          gl.bufferData(gl.ARRAY_BUFFER, array.subarray(1, 3), gl.STATIC_DRAW);
        })()''')
    finally:
        (await browser.close())
    assert seen == [struct.pack('<ff', -1, 0.5)]


@pytest.mark.skipif(not os.environ.get('PYV8C_ANGLE_DIR'), reason='ANGLE libraries not supplied')
async def test_offscreen_triangle_and_context_cleanup():
    with AngleContext(os.environ['PYV8C_ANGLE_DIR'], 16, 16) as context:
        gl = context.gl
        u, i, p = c.c_uint, c.c_int, c.c_void_p
        create_shader = bind_function(gl, 'glCreateShader', u, u)
        shader_source = bind_function(gl, 'glShaderSource', None, u, i, c.POINTER(c.c_char_p), c.POINTER(i))
        compile_shader = bind_function(gl, 'glCompileShader', None, u)
        shader_status = bind_function(gl, 'glGetShaderiv', None, u, u, c.POINTER(i))
        create_program = bind_function(gl, 'glCreateProgram', u)
        attach = bind_function(gl, 'glAttachShader', None, u, u)
        link = bind_function(gl, 'glLinkProgram', None, u)
        program_status = bind_function(gl, 'glGetProgramiv', None, u, u, c.POINTER(i))
        use = bind_function(gl, 'glUseProgram', None, u)
        create_buffers = bind_function(gl, 'glGenBuffers', None, i, c.POINTER(u))
        bind_buffer = bind_function(gl, 'glBindBuffer', None, u, u)
        buffer_data = bind_function(gl, 'glBufferData', None, u, c.c_ssize_t, p, u)
        attribute = bind_function(gl, 'glGetAttribLocation', i, u, c.c_char_p)
        enable_attribute = bind_function(gl, 'glEnableVertexAttribArray', None, u)
        pointer = bind_function(gl, 'glVertexAttribPointer', None, u, i, u, c.c_ubyte, i, p)
        viewport = bind_function(gl, 'glViewport', None, i, i, i, i)
        draw = bind_function(gl, 'glDrawArrays', None, u, i, i)
        read = bind_function(gl, 'glReadPixels', None, i, i, i, i, u, u, p)
        shaders = []
        for kind, source in ((0x8B31, b'attribute vec2 pos; void main(){gl_Position=vec4(pos,0.,1.); }'),
                             (0x8B30, b'precision mediump float; void main(){gl_FragColor=vec4(1.,0.,0.,1.); }')):
            shader = create_shader(kind)
            text = c.c_char_p(source)
            shader_source(shader, 1, c.byref(text), None)
            compile_shader(shader)
            status = i()
            shader_status(shader, 0x8B81, c.byref(status))
            assert status.value, 'GLES shader compilation failed'
            shaders.append(shader)
        program = create_program()
        for shader in shaders:
            attach(program, shader)
        link(program)
        status = i()
        program_status(program, 0x8B82, c.byref(status))
        assert status.value, 'GLES program linking failed'
        use(program)
        buffer = u()
        create_buffers(1, c.byref(buffer))
        bind_buffer(0x8892, buffer.value)
        vertices = (c.c_float * 6)(-1, -1, 1, -1, 0, 1)
        buffer_data(0x8892, c.sizeof(vertices), vertices, 0x88E4)
        index = attribute(program, b'pos')
        assert index >= 0
        enable_attribute(index)
        pointer(index, 2, 0x1406, 0, 0, None)
        viewport(0, 0, 16, 16)
        draw(0x0004, 0, 3)
        pixel = (c.c_ubyte * 4)()
        read(8, 8, 1, 1, 0x1908, 0x1401, pixel)
        assert list(pixel) == [255, 0, 0, 255]
        context.resize(32, 32)
        viewport(0, 0, 32, 32)
        draw(0x0004, 0, 3)
        read(16, 16, 1, 1, 0x1908, 0x1401, pixel)
        assert list(pixel) == [255, 0, 0, 255]
        assert (context.width, context.height) == (32, 32)
        with ThreadPoolExecutor(max_workers=1) as executor:
            with pytest.raises(AngleError, match='creating thread'):
                executor.submit(context.close).result()
    context.close()


@pytest.mark.skipif(not os.environ.get('PYV8C_ANGLE_DIR'), reason='ANGLE libraries not supplied')
async def test_closing_one_context_preserves_the_other():
    with AngleContext(os.environ['PYV8C_ANGLE_DIR']) as first:
        second = AngleContext(os.environ['PYV8C_ANGLE_DIR'])
        second.close()
        first.activate()
        assert first._current_context() == first.context


@pytest.mark.skipif(not os.environ.get('PYV8C_ANGLE_DIR'), reason='ANGLE libraries not supplied')
@pytest.mark.parametrize('automatic', [False, True])
async def test_page_webgl_triangle_uses_angle(automatic):
    from pyv8c.async_api import Browser
    if automatic:
        pytest.importorskip('comfy_angle')
        options = {'webgl_backend': 'angle'}
    else:
        options = {'angle_library_dir': os.environ['PYV8C_ANGLE_DIR']}
    browser = (await Browser(real_network=False, **options).__aenter__())
    try:
        assert not browser.bindings.overrides
        page = browser.new_profile().new_page()
        result = page.evaluate('''(() => {
          const canvas = document.createElement('canvas'); canvas.width = 16; canvas.height = 16;
          const gl = canvas.getContext('webgl');
          const vs = gl.createShader(gl.VERTEX_SHADER);
          gl.shaderSource(vs, 'attribute vec2 pos; void main(){gl_Position=vec4(pos,0.,1.);}');
          gl.compileShader(vs);
          const fs = gl.createShader(gl.FRAGMENT_SHADER);
          gl.shaderSource(fs, 'precision mediump float; uniform float intensity; void main(){gl_FragColor=vec4(intensity,0.,0.,1.);}');
          gl.compileShader(fs);
          const program = gl.createProgram(); gl.attachShader(program, vs); gl.attachShader(program, fs);
          gl.linkProgram(program); gl.useProgram(program);
          const uniform = gl.getUniformLocation(program, 'intensity');
          if (uniform === null || gl.getUniformLocation(program, 'missing') !== null) throw new Error('uniform lookup');
          gl.uniform1f(uniform, 1.0);
          const buffer = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
          gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1,-1,1,-1,0,1]), gl.STATIC_DRAW);
          const location = gl.getAttribLocation(program, 'pos');
          gl.enableVertexAttribArray(location); gl.vertexAttribPointer(location, 2, gl.FLOAT, false, 0, 0);
          gl.drawArrays(gl.TRIANGLES, 0, 3);
          const pixel = new Uint8Array(4); gl.readPixels(8, 8, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, pixel);
          const first = Array.from(pixel);
          gl.clearColor(0, 0, 0, 0); gl.clear(gl.COLOR_BUFFER_BIT);
          gl.viewport(0, 0, 1, 1); gl.drawArrays(gl.TRIANGLES, 0, 3);
          gl.readPixels(8, 8, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, pixel);
          return [first, Array.from(pixel)];
        })()''')
        assert result == [[255, 0, 0, 255], [0, 0, 0, 0]]
    finally:
        (await browser.close())
    assert not browser._graphics_backend._contexts


@pytest.mark.skipif(not os.environ.get('PYV8C_ANGLE_DIR'), reason='ANGLE libraries not supplied')
async def test_angle_state_shader_buffer_and_canvas_encoding():
    import base64
    from pyv8c._native._native import RasterImage
    from pyv8c.async_api import Browser

    browser = (await Browser(real_network=False, angle_library_dir=os.environ['PYV8C_ANGLE_DIR']).__aenter__())
    try:
        page = browser.new_profile().new_page()
        result = page.evaluate('''(() => {
          const canvas = document.createElement('canvas'); canvas.width = 2; canvas.height = 2;
          const gl = canvas.getContext('webgl');
          gl.clearColor(1, 0, 0, 1); gl.clear(gl.COLOR_BUFFER_BIT);
          const pixel = new Uint8Array(4); gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, pixel);
          const shader = gl.createShader(gl.VERTEX_SHADER);
          gl.shaderSource(shader, 'this is invalid'); gl.compileShader(shader);
          const invalid = [gl.getShaderParameter(shader, gl.COMPILE_STATUS), gl.getShaderInfoLog(shader).length > 0];
          const buffer = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
          gl.bufferData(gl.ARRAY_BUFFER, new Uint8Array([9, 1, 2, 3]).subarray(1), gl.STATIC_DRAW);
          const firstSize = gl.getBufferParameter(gl.ARRAY_BUFFER, gl.BUFFER_SIZE);
          gl.bufferData(gl.ARRAY_BUFFER, 12, gl.STATIC_DRAW);
          const size = gl.getBufferParameter(gl.ARRAY_BUFFER, gl.BUFFER_SIZE);
          gl.deleteBuffer(buffer); gl.deleteShader(shader);
          return [Array.from(pixel), invalid, firstSize, size, gl.isBuffer(buffer),
                  gl.getShaderParameter(shader, gl.DELETE_STATUS), canvas.toDataURL()];
        })()''')
        assert result[:6] == [[255, 0, 0, 255], [False, True], 3, 12, False, True]
        image = RasterImage.decode(base64.b64decode(result[6].split(',')[1]))
        assert image.read(0, 0, 1, 1) == bytes((255, 0, 0, 255))
        page.evaluate('''window.testGL = document.createElement('canvas').getContext('webgl');
          window.testBuffer = testGL.createBuffer(); testGL.bindBuffer(testGL.ARRAY_BUFFER, testBuffer);
          testGL.bufferData(testGL.ARRAY_BUFFER, new Uint8Array([1, 2]), testGL.STATIC_DRAW);''')
        receiver = next(context for context in browser._graphics_backend._contexts
                        if context._bound.get(0x8892) is not None)
        buffer = receiver._bound[0x8892]
        native_handle = buffer._angle_buffer
        page.evaluate('testGL.bufferData(testGL.ARRAY_BUFFER, new Uint16Array([1, 2, 3]), testGL.STATIC_DRAW)')
        assert buffer._angle_buffer == native_handle
        assert buffer.size == 6
        page.evaluate('testGL.deleteBuffer(testBuffer)')
        assert buffer._angle_buffer == 0
        page.evaluate('''window.offscreenResult = 'pending';
          const offscreen = new OffscreenCanvas(2, 2);
          const offscreenGL = offscreen.getContext('webgl');
          offscreenGL.clearColor(0, 1, 0, 1); offscreenGL.clear(offscreenGL.COLOR_BUFFER_BIT);
          offscreen.convertToBlob().then(blob => window.offscreenResult = [blob.type, blob.size > 0]);''')
        (await page.run_until_idle(timeout=2))
        assert page.evaluate('window.offscreenResult') == ['image/png', True]
    finally:
        (await browser.close())
