# DevTools 架构与迁移

**简体中文** | [English](../en/pyv8/devtools.md)

## 公开接口

```python
from pyv8c import pyv8

class Window:
    pass

context = pyv8.Context(Window())
try:
    options = pyv8.DevToolsOptions(port=9005, startup_timeout=60)
    with pyv8.start_devtools(context, options=options) as handle:
        print(handle.url)
        result = handle.run(lambda ctx: ctx.exec_js(
            "globalThis.debugValue = 42", filename="example.js"
        ))
finally:
    context.close()
```

`start_devtools()` 只启动后台监听，不执行回调、不打印、不接管信号。独立 Context 没有 executor，
所以 `handle.run(callback)` 在创建 Handle 的调用线程上处理握手，随后调用 `callback(context)` 并返回其结果。
回调中的暂停也由这个线程处理；回调结束不自动关闭服务。再次调试时可再次调用 run。
独立 Context 的 run、wait_for_ready、close 应在这个调用线程上使用；socket 等待可在其他线程使用。

| 配置 | 默认值 | 行为 |
| --- | --- | --- |
| host | 127.0.0.1 | 本地监听，无认证，不应向不可信网络开放 |
| port | 0 | 自动分配端口，通过 Handle.port 获取 |
| wait_for_debugger | False | Browser 是否在执行入口等待就绪；独立 Context 的 run 始终先处理握手 |
| pause_on_start | True | 调试就绪后，下一段宿主脚本的第一条可执行语句暂停一次 |
| startup_timeout | None | 仅限制显式启动等待；None 表示不限时 |

`wait_for_connection(timeout)` 等待 socket，`wait_for_ready(timeout)` 等待 Runtime.runIfWaitingForDebugger；
成功返回 True，超时或服务关闭返回 False。连接不等于调试就绪。run 启动超时抛出 TimeoutError，
不会调用 callback。Browser 对应入口超时不会执行所等待的脚本，也不会关闭页面。

首句暂停在编译成功后的执行边界设置，不插入 debugger、不重新编译已编译的 Script。
Console 的 Runtime.evaluate 不消耗首句暂停。默认每个 Target 生命周期只消耗一次；重连不重复。
低层 Debugger.schedule_pause_on_next_statement(reason) 保存自定义 reason，支持取消与编译失败后重试。

## 分层与线程

| 层 | 文件 | 职责 |
| --- | --- | --- |
| V8 Inspector | src/tools/debugger.cpp | 每 Isolate 一个 InspectorHub，Context 独立 group，session 独立原生会话 |
| 原生进入调度 | src/isolate_scope.cpp、Context._try_inspector | 非阻塞进入 Isolate，忙时登记释放通知 |
| 调度 | pyv8/devtools/scheduler.py | 每 Isolate 一个邮箱，启动等待和嵌套暂停共享命令，不搬运临时队列 |
| 会话与执行状态 | session.py | ready、启动等待、首句暂停、原生会话关闭 |
| 协议路由 | router.py、requests.py | Target/flattened session、连接代次、请求关联、发现 |
| 传输 | transport.py | HTTP/WebSocket、单客户端、连接代次；不进入或等待 V8 |
| 宿主适配 | CallingThread / pyv8c.platform.devtools | 显式 Context、executor、URL、父目标和关闭回调 |

普通 Inspector 命令在目标宿主线程上执行；取得 Isolate 前，命令仍留在邮箱。
若 Isolate 忙，使用释放通知唤醒投递，避免其他线程拿走命令后阻塞于 V8 锁。
暂停通知先标记 Isolate 的暂停线程，再发送给客户端。暂停线程进入命令所对应的 Context，
统一处理该 Isolate 的 Inspector 命令；不运行宿主任务队列。嵌套暂停使用栈式等待条件。
恢复后才运行排队的销毁任务，不能在原生暂停栈上销毁最后一个 Inspector。

## 发现、会话与重连

- HTTP `/json`、`/json/list` 返回目标描述、WebSocket 地址及前端地址；`/json/version` 返回协议和 V8 信息。
- WebSocket 支持 `/devtools/browser/<service-id>`、`/devtools/page/<target-id>`，`/` 指向根目标。
- 第二个客户端以 WebSocket 1013 拒绝。没有多客户端协作或认证协议。
- `Target.setAutoAttach` 仅支持 flatten=true。自动附加作用域是当前父 session 的直接子目标；
  子 session 可自行开启下一层自动附加。关闭父 session 会分离其自动附加子 session。
- 既有子目标附加时不倒退到启动等待。新子目标在执行启动脚本前登记；
  waitForDebuggerOnStart 决定是否等待，attachedToTarget.waitingForDebugger 报告实际状态。
- 每个显式附加 session 都有独立原生 V8InspectorSession。内部编号全局递增，关联 session、
  原始请求编号和 session 所属连接代次；迟到响应、断线队列和旧 session 不会进入新连接。
- 断线先使旧 session 失效、清空请求映射、唤醒启动等待并恢复暂停，再释放原生会话；
  不关闭宿主或监听服务。重新连接后由前端重新启用 Debugger 和设置断点，不重放旧命令。

关闭顺序：拒绝请求 → 唤醒等待/恢复执行 → 合法线程释放 session/Inspector → 停止网络服务。
Handle.close 幂等；失败会抛出可观察的 RuntimeError/TimeoutError，而不是在错误线程上强行释放。
关闭 Handle 不关闭宿主 Context。仍须遵守 Context/Script/JSObject 的 Isolate 生命周期约束。
低层原生 API 不允许在活跃 Inspector 协议/暂停回调内直接 disconnect 或关闭 Context；
应先 resume，待回到宿主执行线程后关闭。共享服务自动安排这个顺序。

## 旧接口迁移

| 旧用法 | 新用法 |
| --- | --- |
| start_devtools(ctx, callback=fn, port=9005) | handle = start_devtools(ctx, options=DevToolsOptions(port=9005)); handle.run(fn) |
| context.start_devtools(page, host=..., port=...) | context.start_devtools(page, options=DevToolsOptions(host=..., port=...)) |
| start_paused=True | DevToolsOptions(wait_for_debugger=True) |
| handle.wait / 隐式进程信号接管 | 调用者维护程序生命周期，使用 close/上下文管理 |
| DevtoolsDebugger / DevToolsServer / DevToolsTarget | 不再公开；使用 DevToolsOptions 和 DevToolsHandle |
| Debugger.run_js / 隐式首句暂停 | on_ready 回调只报告就绪；低层显式 schedule_pause_on_next_statement |

不提供第二套兼容实现。支持 Browser、Debugger 与上述 Target 子集，不实现完整 Page、Network、DOM 检查。
