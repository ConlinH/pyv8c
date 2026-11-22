# Browser 与同线程执行内核

公开对象为 **Browser → Profile → Page**。Browser 持有配置和 Profile；Profile
隔离 cookies、存储和网络会话；Page 提供页面操作。内部 Runtime 通过组合持有，
只管理执行器、线程归属和后台资源回收，不作为应用入口。详细规则见
[接口契约](api-contract.md)和[使用指南](usage.md)。


同步脚本使用独立入口，导航、等待、输入、网络和关闭均直接返回：

```python
from pyv8c.sync_api import Browser

with Browser(real_network=False) as browser:
    with browser.new_profile() as profile:
        with profile.new_page() as page:
            page.set_content("<title>Local page</title>")
            page.evaluate("globalThis.ready=false;setTimeout(()=>ready=true,2)")
            page.wait_for("ready", timeout=1)
            assert page.evaluate("document.title") == "Local page"
```

同步 Browser 在调用线程持有一个事件循环，等待时协作推进，evaluate 直接执行。
`time.sleep()` 不推动页面任务。已有 asyncio 循环使用下面的 async_api，禁止
嵌套同步等待；根包不再提供 Browser/Profile/Page/run 别名。两套入口共用内核，
不互传对象。同步关闭失败时保留循环与所有权，允许 close 重试。

下面的示例使用异步入口；同步入口使用相同方法名并去掉 await，
上下文管理使用 with。两套入口中的 evaluate 都同步返回。

```python
import asyncio
from pyv8c.async_api import Browser

async def main():
    async with Browser(real_network=False) as browser:
        async with browser.new_profile() as profile:
            async with profile.new_page() as page:
                await page.set_content("<title>Local page</title>")
                assert page.evaluate("document.title") == "Local page"

asyncio.run(main())
```

`evaluate` 和 `evaluate_handle` 同步返回，每次保留 JS 与微任务的执行边界，
不等待 Promise。`goto`、`set_content`、输入、网络和关闭使用 await。
`set_content` 默认等到 load；需要观察解析中的文档时显式选择 wait_until="commit"。
查询和即时状态读取保持同步。`run_until_idle` 只等待可运行任务排空，不代表远期
计时器、网络或业务完成。Page 不暴露 Runtime 对象。

Page 和 iframe 的 V8 isolate 固定在应用线程，Worker 使用独立线程。
跨线程直接操作 Page 会报错；内部 I/O、Worker 和 Inspector 使用队列交付结果。
每个同步 Browser 只驱动自己的循环；多个异步 Browser 可在同一 asyncio 循环中并发，
生命周期相互独立。JS 活句柄仅在所属线程和文档内有效。同步 Python 回调和持续计算会占住该线程；通过 await 交还调度权。
禁止在活跃 JS 栈里挂起宿主等待，也不建立嵌套事件循环。

标准 asyncio.run 可直接使用，不修改正在运行的循环时钟。可选的
`from pyv8c.async_api import run` 在创建循环时选择高精度时钟，适用于计时敏感的命令行
程序。普通循环的唤醒粒度由 Python 和操作系统决定；内部期限仍使用单调时钟。
Inspector 暂停时，调试控制端须独立于业务循环；暂停不消耗 JS 执行预算。

`await page.close()` 等待该 Page 的宿主 HTTP、加载回调、子 frame 和 Worker；
`await profile.close()` 再释放共享网络会话；Browser 退出最后汇合执行内核和
共享加载池。关闭调用幂等，取消一个等待者不取消真实清理。失败保留所有权并
允许重试；两个内建 HTTP 引擎的请求通道都通过取消请求协程来尝试真实终止在途调用
（连接阶段、等待响应头与零数据体同样生效），但在途网络等待的预算耗尽同样抛
`HostStateError`、保留在途计数并等待下一次 `close()`。`page.closed` 表示 JS 上下文
已退休；后台工作结束以 await close 完成为界。
DevTools 和解析器使用内部原子退休接口，不把 coroutine 交给同步 JS 回调。

运行 `python examples/browser/cooperative.py` 可离线验证导航、fetch、timer、
Worker 和关闭。回归入口为 tests/integration/test_browser_api_contract.py，
并继续执行同线程内核、导航、超时和 DevTools 专项用例。基准使用统一入口
`python benchmarks/benchmark.py --cases execution_concurrent,execution_timer_load,entrypoint_async_evaluate --runs 5 --output build/cooperative-measurement.json`
测量新进程中的协作执行；`--list` 可查看全部用例。比较时核对源码、工作负载和原生二进制摘要。本阶段按用户要求不验证 Linux/macOS。
