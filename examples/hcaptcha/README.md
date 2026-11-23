# hCaptcha

```powershell
..\.venv\Scripts\python.exe -X utf8 -B examples/hcaptcha/hcaptcha.py
```

在仓库根目录运行，默认代理 `http://127.0.0.1:6696`、等待预算 60 秒。支持 `--proxy none`、`--timeout`。

在 dashboard 登录页的 origin 下加载当次官方 SDK，通过 `hcaptcha.render()` 和 `hcaptcha.execute()` 执行 invisible 流程。SDK 出错或未返回 `P1_` Token 时失败。

`await run(proxy=..., timeout=...)` 返回 `token`、`elapsed`。CLI 打印 site key、完整 Token、前缀、长度及耗时，不打印 Cookie，不落盘。已移除用于分析的传输层日志，包括 SDK 配置、请求详情与完整 `req`。

本例以取得 `P1_` Token 为结束条件，不调用账号登录接口；服务端若要求交互挑战，本例会失败或超时。

## 2026-11-23 修复

`challenge-error` 的本地复现发生在 iframe 初始化阶段：当前 `hcaptcha.html` 用 CSP 的 SHA-256 哈希授权内联脚本，脚本与声明哈希一致，但框架此前只处理 nonce，未实现哈希匹配，因此脚本没有执行，配置请求也未发出。

框架现按 [CSP 内联脚本匹配规则](https://www.w3.org/TR/CSP3/#match-element-to-source-list) 校验源码的 SHA-256、SHA-384、SHA-512，保留多策略交集及指令优先级；改动源码后哈希不符的脚本仍被拦截。案例白名单同时补充了 SDK 使用的备用接口 `api2.hcaptcha.com`，仅放行原有的 `/checksiteconfig` 和 `/getcaptcha/…` 路径。

验证：CSP 与 Trusted Types 相关测试 42 项通过，项目策略检查 6/6 通过。MCP 浏览器中，三种哈希的匹配／改动源码共 6 项结果与本地一致。使用默认 6696 代理，本机直接调用正式 `run()` 取得 2439 字符的 `P1_` Token，耗时 8.65 秒；独立 CLI 取得 2450 字符的 Token，耗时 12.24 秒、退出码 0；另一次 CLI 等待约 90 秒后超时、退出码 1。上述两次通过均使用实时 SDK，不包含调试阶段的缓存资源实验。浏览器和历史 HAR 仅用于分析，运行不读取其中的 Cookie、Token、SDK 或请求参数。

诊断期间也观察到代理连接上的间歇性 TLS 中断；这是独立的网络失败，仍会按错误或超时返回，不将其当作验证通过。程序不自动重放校验 POST。
