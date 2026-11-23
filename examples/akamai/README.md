# Akamai / Korean Air

```powershell
..\.venv\Scripts\python.exe -X utf8 -B examples/akamai/akm.py
```

在仓库根目录运行，默认代理 `http://127.0.0.1:6696`、等待预算 60 秒。支持 `--proxy none`、`--timeout`。

打开 Korean Air，按原案例移动鼠标并点击，等待当前 Profile 中的 `_abck` Cookie 包含 `~0~`。满足后退出码为 0；未取得该值时超时失败，不将空 Cookie 当作成功。

`await run(proxy=..., timeout=...)` 返回 `cookies`、`elapsed`。终端打印进度、完整 Cookie 和耗时，不生成日志文件。

沿用原案例的资源筛选：阻止图片、样式、含 `/akam/` 或 `.js` 的请求。该筛选依赖站点现有入口结构；SDK 地址变化时可能无法取得 Cookie。本例只检查 Cookie 标记，未追加业务接口验收，本次简化不代表线上验证已通过。
