# SteamDB / 5s

异步入口为 `5s.py`，同步入口为 `steamdb.py`，两者流程一致：打开登录页、通过 deep 接口定位复选框、点击后观察页面标题变化。

```powershell
..\.venv\Scripts\python.exe -X utf8 -B examples/5s/5s.py
..\.venv\Scripts\python.exe -X utf8 -B examples/5s/steamdb.py
```

在仓库根目录运行。默认代理 `http://127.0.0.1:6696`，等待预算 120 秒；支持 `--proxy none` 直连与 `--timeout 120`。

入口使用 `Browser(gpu_enabled=False)`，保留挑战域资源，裁剪站点的图片、样式、字体和媒体。同步入口使用 `page.sleep()` 推进页面任务。日志输出加载、点击、标题变化、目标站点完整 Cookie 和耗时。`run(proxy=..., timeout=...)` 返回 `elapsed`。

目前精简入口在点击后检测到标题变化即返回；HTTP 状态、响应正文及登录控件的完整验收需另外核对。旧实现于 2026-11-18 曾取得 `Sign In · SteamDB`、HTTP 200、35,681 字节的登录文档，这是历史验证，不是当前入口的返回结构或本次线上复测结果。

运行所需脚本由框架下载，不读取 HAR、历史 Cookie 或浏览器自动化产物。GPU 选项统一关闭页面、iframe 与 Worker 的 WebGL/WebGPU 能力，保留 CPU Canvas 2D。
