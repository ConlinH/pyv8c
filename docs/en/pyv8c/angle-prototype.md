# Experimental ANGLE WebGL backend

[简体中文](../../pyv8c/angle-prototype.md) | **English**

Install the optional dependency and enable it explicitly:

```bash
python -m pip install "pyv8c[angle]"
```

```python
from pyv8c.async_api import Browser, run

async def main():
    async with Browser(webgl_backend='angle') as browser:
        page = browser.new_profile().new_page()
run(main())
```

The simulated backend remains the default. When ANGLE is selected, the EGL and GLES libraries come from `comfy-angle`; if it is not installed,
an explicit message prompts you to install `pyv8c[angle]`. `angle_library_dir` can specify another library directory and stays compatible with the older
explicit-directory usage.

This is an experimental subset of WebGL 1, supporting only the minimal path of page-canvas shader linking, `uniform1f`, float vertex buffers, vertex attributes,
`drawArrays`, and RGBA unsigned-byte `readPixels`. Other methods, WebGL 2, Worker,
Canvas export, full parameter and resource validation, error semantics, and context-loss handling are not yet complete. It directly executes native
graphics calls, has no browser-level isolation, and should not run arbitrary shaders from untrusted pages.

## Validation

`comfy-angle` ships wheels for platforms such as Windows x64, Linux x64, and macOS arm64; a real headless context
still depends on the operating system and graphics runtime. Run the integration tests with a specified library directory:

```powershell
$env:PYTHONPATH = 'pyv8c'
$env:PYV8C_ANGLE_DIR = (python -c "import comfy_angle; print(comfy_angle.get_lib_dir())")
python -m pytest pyv8c/tests/integration/test_angle_context.py -q
```

The tests cover offscreen drawing, page JS drawing, pixel reads, context resizing, and owner-thread release.
Ubuntu 22.04 x64 has been verified over SSH with no desktop environment and Mesa llvmpipe software rendering; Linux prefers
`EGL_MESA_platform_surfaceless`. Hardware GPUs, other Linux configurations, and macOS still need separate verification.
