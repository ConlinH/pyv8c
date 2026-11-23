# reCAPTCHA / Check-in Pax

```powershell
..\.venv\Scripts\python.exe -X utf8 -B examples/recaptcha/recaptcha.py
```

在仓库根目录运行，默认代理 `http://127.0.0.1:6696`、等待预算 90 秒。支持 `--proxy none`、`--timeout`。

1. 读取实时首页与应用脚本中的公开 site key。
2. 在应用 origin 下加载当次 Google SDK，等待初始化，再执行原应用的 `grecaptcha.execute(siteKey, {action:'submit'})`。
3. 核对 Token 确实来自本次 `/recaptcha/api2/reload` 的成功 `rresp`，拒绝 SDK 的错误回退字符串。
4. 将 Token 与固定无效测试凭据提交到登录接口。要求 HTTP 200 且消息精确为 `Invalid email or password.`。

响应中的 `success:false` 是预期的账号密码错误，不代表本例失败。本例不使用真实账号。

`await run(proxy=..., timeout=...)` 返回 `response`、`token`、`elapsed`。终端打印关键进度、当次 site key、完整 `reCaptchaToken`、登录响应和耗时，不打印 Cookie，不落盘。

已移除子进程包装、手写 GET 重试、独立静态缓存及逐请求日志，网络请求使用框架传输层。POST 不增加自动重试。原先文档提到的 `RECAPTCHA_PROXY` 未由代码读取，现统一使用 `--proxy`。

历史实现曾取得预期登录响应；本次简化的线上结果需以当次完整请求为准，不能只凭 HTTP 200 或任意 Token 字符串判定成功。
