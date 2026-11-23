# Kasada / OTTO 搜索

```powershell
..\.venv\Scripts\python.exe -X utf8 -B examples/kasada/otto.py
```

在仓库根目录运行，默认查询 `https://www.otto.de/suche/banana/`，代理 `http://127.0.0.1:6696`，等待预算 65 秒。支持 `--proxy none`、`--timeout`、`--url`；URL 限定为 `www.otto.de` 的 HTTPS 地址。

框架加载当次原始 SDK，由 SDK 完成 `/tl` 请求和重载。`/tl` 明确拒绝时失败；最终要求搜索文档 HTTP 200，并从当前页面 DOM 提取到商品。首次直接取得搜索页 200 也可正常返回。

`await run(url=..., proxy=..., timeout=...)` 返回 `products`、`html_bytes`、`elapsed`。每条商品包含名称、ID、规格 ID、链接、位置、广告标记、价格与图片等信息；按规格 ID（缺省时按商品 ID）去重，保留页面顺序。价格以 Decimal 将分换成两位小数，占位卡片缺失的价格保留 `None`。

CLI 打印关键进度、`/tl` 的完整 `x-kpsdk-*` 头与荷载、商品总数及前 10 条名称、价格和链接，不打印 Cookie。二进制荷载以 Python 字节字面量完整显示。已去掉请求体哈希、事件报告与 `--environment`；不保存 HTML、SDK 或日志文件。

历史实现于 2026-11-16 取得过搜索页并提取到 120 条商品。本次使用页面 DOM 简化商品解析，历史数量不作为当前运行的固定预期，线上结果以当次请求为准。
