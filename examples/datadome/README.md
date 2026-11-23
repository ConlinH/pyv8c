# DataDome / Neiman Marcus

```powershell
..\.venv\Scripts\python.exe -X utf8 -B examples/datadome/neimanmarcus.py
```

在仓库根目录运行，默认代理 `http://127.0.0.1:6696`、等待预算 60 秒。支持 `--proxy none`、`--timeout`。

1. 打开首页，加载当前 interstitial SDK。
2. 在提交边界取得 SDK 当次生成的原始请求，检查必需参数，打印完整 `payload`、`plv3`。
3. 由宿主原样提交这次 Interstitial 请求一次，要求 HTTP 200，读取响应 `cookie` 字段中的非空 `datadome` 值并打印。
4. 拿到三个参数后立即结束，不写入返回的 Cookie，不再携带它访问首页，不做页面正文或放行验收。

`await run(proxy=..., timeout=...)` 返回 `payload`、`plv3`、`datadome`、`elapsed`。CLI 成功时只打印三个参数的完整原值：

```text
payload: …
plv3: …
datadome: …
```

`datadome` 仅含 Cookie 的值，不含 `datadome=` 前缀或 Domain、Path、Max-Age 等属性。`view=captcha` 或其他 `view` 不再作为失败条件：这里只交付参数，不表示站点已放行，也不跟随响应中的验证码 URL。

成功取得三个非空参数时退出码为 0。缺失参数、无挑战可生成参数、HTTP 错误或响应缺少有效 `datadome` 时返回 1。保留总流程超时，不保存 HTML、SDK、参数或日志文件。
