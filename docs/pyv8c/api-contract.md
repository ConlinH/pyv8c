# Browser / Profile / Page 接口契约

本契约是公开 Python API 的设计依据。项目尚未发版，调用方直接迁移，
提供 `pyv8c.sync_api` 和 `pyv8c.async_api` 两套明确入口，共享同线程 Runtime。
根包不再导出 Browser、Profile、Page 或 run；不提供旧入口别名或模式自动切换。

## 对象和所有权

`async with Browser(...) as browser` 在当前 asyncio 循环和线程上启动浏览器。
`browser.new_profile()` 同步创建隔离 cookies、存储和网络会话的 Profile；
`profile.new_page()` 同步创建 about:blank Page。`browser.new_page()` 使用该
Browser 的默认 Profile。Page 暴露 `profile`，Profile 暴露 `browser`。

Browser 通过组合持有内部 Runtime。Runtime 只负责执行器、线程归属、
调度和后台资源回收；不持有浏览器配置，不创建 Profile，不对外导出。
浏览器配置、绑定、Profile 集合由 Browser 拥有。Worker 保持独立线程。

所有公开 Browser/Profile/Page 操作在启动 Browser 的线程执行；异步操作还
必须在同一事件循环执行。禁止跨线程直接调用，也禁止在 JS 栈中挂起等待。
宿主资源加载回调是同步协议，只交换 Python 请求/响应数据；在线程池内
执行时不能调用 Page API。需要自行访问网络时显式使用 transport 请求/响应协议。

## 入口与操作边界

同步应用使用 `from pyv8c.sync_api import Browser` 和 `with Browser(...)`；
异步应用使用 `from pyv8c.async_api import Browser` 和 `async with Browser(...)`。
两者均为 Browser → Profile → Page，页面会话、资源与执行内核只有一份实现。
同步接口的导航、等待、输入、HTTP 和关闭直接返回结果；下表描述异步入口。
两种入口中的 evaluate、evaluate_handle、查询和状态读取都同步完成。
同步 Profile/Page 通过所属 Browser/Profile 的工厂创建，不直接构造。

| 操作 | 调用方式 | 返回边界 |
| --- | --- | --- |
| 创建 Profile / 空白 Page | 同步 | 对象可用，不加载 URL |
| evaluate / evaluate_handle / 状态读取 | 同步 | 本次 JS 和其微任务检查点结束；不等待 JS Promise |
| query_selector（含 deep）/ dispatch_event / set_visibility | 同步 | 当前查询、事件或状态变更及检查点完成 |
| goto / set_content | await | 到达 wait_until 指定的 commit、domcontentloaded 或 load |
| wait_for / wait_for_selector / wait_for_load_state / run_until_idle | await | 条件满足或截止时间到达 |
| sleep | await（同步入口直接返回） | 固定时长经过；期间页面事件循环继续推进，不执行 JS |
| 鼠标和 Page 输入操作 | await | 事件及检查点结束；时长通过协作式等待推进 |
| page.net.request/get/post/... | await | 宿主 transport 返回完整响应 |
| page.net.fetch/xhr | await | 页面 JS API 返回响应，包含页面安装的包装逻辑 |
| Browser / Profile / Page.close | await | 停止事件源、释放原生对象，并等待所拥有的后台工作完成 |

`set_content` 默认等待 load；`wait_until="commit"` 允许解析/资源尚未完成时
检查页面。内部解析器和 JS 回调通过内部提交/退休方法实现原子操作，不反向
调用公开 coroutine。内部退休与公开 close 使用同一个异步收尾任务，父 Page
等待各子 Page 的关闭完成，再释放自己的后台资源。网络不存在公开的
concurrent Future 或 `*_async` 变体。

`query_selector()` 与 `wait_for_selector()` 的 `deep=True` 是宿主特权查询：依次搜索本页 light DOM、各 shadow tree（含 closed）、再按 DOM 顺序递归子 frame，返回 tree order 首个匹配。`click()` / `dblclick()` / `hover()` 的 `deep=True` 使用同一解析，并把输入交给节点所属 Page 的鼠标；返回句柄仍受该 realm 的线程归属约束。组合子不跨 shadow 边界，`:host` / `::slotted` 未实现。详见[使用说明](usage.md)。

## 持久化 Profile

`browser.new_profile(user_data_dir=..., persist_session_cookies=False)` 在两套入口都是同步工厂。指定目录时，先获取独占锁，再恢复 Cookie 和 HTTP(S) `localStorage`。目录正在使用、数据库损坏或格式版本不受支持时抛出 `HostStateError`。省略目录时创建内存 Profile，`browser.new_page()` 的默认 Profile 也属于这一类。

同一 Profile 的同源页面共享 `localStorage` 数据，各 Window 持有自己的包装对象。跨源导航切换数据源，旧对象仍指向原 origin。同步 `profile.flush()` 和异步 `await profile.flush()` 保证调用前的持久化变更已提交，内存 Profile 的 `flush()` 直接返回。关闭先收尾页面和网络，再保存。保存失败保留数据和目录锁，调用方可以重试 `close()`。进程异常退出后恢复最后一次成功提交的状态。

## 超时、取消和关闭

秒为统一时间单位。超时预算包含操作启动、JS 执行和等待；同步入口进入
持久循环前的排队也消耗同一预算，开始操作前扣除已消耗时间；微任务继承当前
执行预算。宿主 HTTP 的预算还包括请求编码与 Profile HTTP 调度排队；提交及开始传输
前都检查剩余预算，已耗尽的请求不发送。同步 JS 执行使用原生终止机制，
不通过修改 V8 实现。
超时只取消当前观察/操作，不自动关闭 Browser。导航取消阻止尚未提交的迟到
响应覆盖页面；已经提交的文档不会回滚。内容注入和输入取消也不回滚已发生
的 DOM 变化或事件；后续步骤停止，取消的点击不合成 click。输入动作在
等待手势锁前绑定文档代次，获得锁后验证。同步入口在调用时绑定代次，
进入事件循环后再次验证，防止已就绪的导航让动作落到新文档。输入事件派发后也会检查代次；如果处理器通过 `document.open()` 等方式同步替换文档，手势会以 `HostStateError` 停止。

关闭幂等。开始关闭后拒绝新工作；一个关闭等待者取消不取消实际清理。
清理失败保留重试入口及所有权，不把尚存活的资源报告为已完全关闭。
Page 关闭仍会派发卸载事件，但这些回调不能启动新的同步资源加载。
关闭阶段耗时记录在带 `close_timing` 字段的日志中；超过 250 ms 的阶段记为警告。
Page 关闭等待自己的加载回调、宿主 HTTP 和 Worker，不等待兄弟 Page 的工作。
Profile 关闭先等待其全部 Page，再释放共享网络会话；Browser 最后等待内部
Runtime 和共享加载池。两个内建 HTTP 引擎（`curl_cffi` 与 `cronet`）的宿主与
资源请求都通过取消专用事件循环上的整个请求协程来尝试真实中断，连接阶段、
等待响应头与零数据体同样可停止；自定义 HTTP 引擎须执行请求截止时间，同步加载回调须自行控制耗时。
传输层通过 `RequestHandle.cancel()` 请求中断。传输完成清理并结算 Future 后，调度器才释放在途计数。
关闭会等待其退出，不宣称取消 Python 线程；关闭预算耗尽抛 `HostStateError` 并允许重试。

Page 关闭允许连续等待 60 秒无进展。观察到网络请求、宿主回调等后台工作完成时，会刷新预算。预算耗尽抛
`HostStateError`，在途计数不释放、页面仍归属原 Profile，可再次 `close()` 重试；
不伪造终态，也不补一次错误结果。预算按单次等待计算，重试获得新预算，持续有
进展的关闭不会被误杀。共享网络会话关闭引擎时，任一引擎失败都会上报，其余引擎
仍被关闭；失败项保留引用，下次 `close()` 只重试它们，全部成功后才置空引用并
标记完成。

`page.closed` 表示 JS 上下文已经退休，后续不能再运行脚本；后台资源全部
结束以 `await page.close()` 完成为界。Browser 的 closed 表示执行内核已回收。

## 事件循环

同步 Browser 在 `with` 进入时创建一个持久事件循环，绑定当前调用线程，
在 Browser 完整关闭后释放。等待、导航、输入和网络操作使用同一循环推进，
同步 evaluate 不启动循环，也不额外增加事件循环往返。退出 Page/Profile 的
上下文只清理该作用域，Browser 循环保持可用。同步关闭在推进循环前停止所属
页面事件源，再等待后台清理。关闭失败或观察者中断时保留循环，允许 close 重试。
启动中断时清理已开启的 Runtime，清理失败时同样保留 close 重试入口。

每次同步等待使用调用时的 ContextVar 快照。操作中断时取消并收尾本次等待任务，
已启动且无法中断的 I/O 和受保护的资源清理任务仍由其所有者负责。

同步入口禁止在已经运行的 asyncio 循环中使用，应改用 async_api；禁止在
任何活跃 JS 栈的 Python 回调中启动同步等待，检查发生在驱动循环之前。
同步与异步对象不能相互传递。每个同步 Browser 只驱动自己的循环：嵌套使用
两个 Browser 不会推进外层 Browser；需要并发运行多个 Browser 时使用异步入口。
宿主 `time.sleep`、阻塞 I/O 或持续计算不推进页面 timer、网络回调和 Worker
消息。使用 page.wait_for、wait_for_load_state 等等待明确的业务条件。


支持标准 `asyncio.run(main())`，不读取私有精度阈值拒绝循环，不修改正在运行
的循环时钟。Python/系统默认循环决定唤醒粒度，内部截止时间仍以单调时钟
检查。命令行可使用 `pyv8c.async_api.run(main())` 创建高精度循环；这是启动辅助函数，
不引入另一种对象模型。

## 验收

Windows 对照冻结的重构前源码，运行相同 workload 和相同原生二进制的基准。
回归包含导航、timer、微任务、fetch/XHR、Worker、DevTools、超时、取消和关闭，
并检查普通 asyncio 循环及已安装 wheel。既有失败单独列出，不改期望掩盖回归。
Linux 和 macOS 本阶段按用户要求暂不验证。
