# 实验性 ANGLE WebGL 后端

**简体中文** | [English](../en/pyv8c/angle-prototype.md)

安装可选依赖并显式启用：

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

默认仍使用模拟后端。选择 ANGLE 时从 `comfy-angle` 获取 EGL 和 GLES 库；没有安装时
会明确提示安装 `pyv8c[angle]`。`angle_library_dir` 可指定其他库目录，并兼容旧的
显式目录用法。

这是 WebGL 1 实验性子集，仅支持页面 canvas 的 shader 链接、`uniform1f`、float 顶点缓冲、顶点属性、
`drawArrays` 和 RGBA unsigned-byte `readPixels` 最小路径。其余方法、WebGL 2、Worker、
Canvas 导出、完整参数与资源校验、错误语义和上下文丢失处理尚未完成。它直接执行原生
图形调用，不具备浏览器级隔离，不应执行不可信页面的任意着色器。

## 验证

`comfy-angle` 提供 Windows x64、Linux x64 和 macOS arm64 等平台 wheel；实际无头上下文
仍依赖操作系统和图形运行环境。指定库目录运行集成测试：

```powershell
$env:PYTHONPATH = 'pyv8c'
$env:PYV8C_ANGLE_DIR = (python -c "import comfy_angle; print(comfy_angle.get_lib_dir())")
python -m pytest pyv8c/tests/integration/test_angle_context.py -q
```

测试覆盖离屏绘制、页面 JS 绘制、像素读取、上下文调整大小以及所属线程释放。
Ubuntu 22.04 x64 已在 SSH 无桌面环境、Mesa llvmpipe 软件渲染下验证；Linux 会优先使用
`EGL_MESA_platform_surfaceless`。硬件 GPU、其他 Linux 配置和 macOS 尚需分别验证。
