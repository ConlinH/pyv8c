# 调试器

**简体中文** | [English](../en/pyv8c/devtools.md)

[文档索引](../README.md)

## 启动与连接

协议、会话、网络服务和 Isolate 邮箱由 `pyv8.devtools` 提供，pyv8c 负责页面、iframe 和 Worker 的目标描述及生命周期接入。

```python
from pyv8c.async_api import Browser, run

async def main():
    from pyv8c import DevToolsOptions
    async with Browser(real_network=False) as browser:
        profile = browser.new_profile()
        page = profile.new_page()
        handle = profile.start_devtools(page, options=DevToolsOptions(pause_on_start=True, wait_for_debugger=True, port=9005))
        print(handle.url)
        page.evaluate('globalThis.answer = 42', filename='example.js')
        input('连接调试器后，按回车结束：')
run(main())
```

DevToolsOptions 默认 host 为 `127.0.0.1`、port 为 0、wait_for_debugger 为 False、pause_on_start 为 True、startup_timeout 为 None。port=0 自动分配端口。

需要客户端就绪后再执行业务时设置 `wait_for_debugger=True`，并设置合适的 startup_timeout。启动握手超时明确抛出 TimeoutError，不将未执行脚本算作成功。`pause_on_start` 控制握手后下一段宿主脚本的首句暂停，初始化 helper 不消耗该暂停机会。

## Handle 与目标

Handle 提供 url、host、port、close、`wait_for_connection(timeout)` 和 `wait_for_ready(timeout)`。同步入口返回的 Handle 会在这两个等待中协作推进 Browser 的循环；
异步入口返回原生 Handle，等待应放在 `await asyncio.to_thread(...)` 中，避免阻塞 owner 循环。
两个等待返回 bool；WebSocket 连通不代表 Debugger 已完成握手。pyv8c 自己拥有执行器，正常调用 `page.evaluate()`，不使用独立原生 Context 的 handle.run 工作流。

服务提供 `/json`、`/json/list`、`/json/version` 和 WebSocket 调试地址。目标 ID 随宿主存活，重连后客户端需重新启用相应域并恢复断点；filename 决定脚本来源名称及 URL 断点匹配。

当前为单客户端服务，第二个 WebSocket 会被拒绝。监听端口没有浏览器级认证保护，应保持本机绑定。

flattened Target.setAutoAttach 按父 session 管理直接子目标。iframe/Worker 在启动脚本前登记，waitForDebuggerOnStart 控制是否等待；附加已有目标不等于重新启动。父 session 分离会清理子会话，父页面关闭会关闭所属子目标。

## 暂停与关闭

同一页面树的 Window 共享 Isolate，Worker 使用独立 Isolate。暂停时由对应暂停线程处理 Inspector 命令，不运行普通页面任务；不相关执行组继续工作。

Inspector 暂停不消耗 JS 执行预算，宿主等待仍有截止时间。退出先解除启动门及暂停，再在合法 owner 线程销毁会话和目标，最后停止服务。正常关闭可重复调用，失败需报告；关闭 Handle 本身不关闭页面。

## 能力范围

支持 V8 Inspector 和框架实现的 Target 管理，不提供完整 CDP Page、Network、DOM 域，也不支持多客户端协同。Chrome 前端能连接不代表全部 Chrome DevTools 功能可用。

专项入口为 `pyv8c/tests/browser/test_devtools.py`、`pyv8c/tests/integration/test_lifecycle_stress.py`。暂停通知、分离和关闭涉及并发，需要重复验证并保留失败证据；复跑通过不能单独证明竞态已解决。
