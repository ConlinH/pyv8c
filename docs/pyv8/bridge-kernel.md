# 桥接内核

**简体中文** | [English](../en/pyv8/bridge-kernel.md)

> 本文记录历史桥接实验与当时的测量命令。当前统一基准入口见 [benchmarks/README.md](../../benchmarks/README.md)，文中的 `benchmarks/bridge.py` 等独立脚本已并入该入口。

桥接要求 CPython 3.12 Stable ABI。在 3.12 上构建的 wheel 面向
同一操作系统和架构上启用了 GIL 的常规 CPython 3.12 至 3.15。
不支持无 GIL（free-threaded）Python 和子解释器。
所选 V8 版本是 15.4.68。

## 值

`Context.exec_js`、JS 函数调用、属性访问和宿主回调参数返回原始值、
原来的 Python 宿主对象或 JS 句柄。它们不枚举 JS 对象，也不调用
访问器来转换它们。`preserve_arguments` 开关和 `_to_python` 方法
已被移除。

```python
result = context.exec_js('({items: [1, 2]})')
assert isinstance(result, pyv8.JSObject)
assert result.to_python() == {'items': [1, 2]}
```

`to_python()` 递归地物化数组和普通对象，保留
环和共享引用。它可以运行 getter 并传播其错误。
其他对象，包括函数和定型数组，仍保持为句柄。
当句柄返回 V8 时保留 JS 标识；不保证 Python 包装器的
标识。函数属性访问保留其接收者。

二进制数据整体跨界，在任一方向上都只需一次调用。`JSObject` 上的
`_bytes()` 返回缓冲区持有的字节或视图所查看的字节；交回 realm 的
Python `bytes` 会作为新的 `ArrayBuffer` 到达。跨边界逐元素读取
缓冲区正是这些所替代的做法。

在 pyv8c 中，`Page.evaluate()` 在页面执行器上物化结果；
`Page.evaluate_handle()` 返回未物化的值。内部 Web API
代码一直使用句柄，直到其 Web IDL 转换明确需要 Python 数据。

## 所有权与进入

`ExecutionScope` 固定 Context 和 subject，获取 Isolate
访问权，并在等待后再次校验 Context。同线程的嵌套进入
复用现有的锁。等待会释放 GIL，V8 运行的时间窗口同样
会释放 GIL：进入 JavaScript 的跨界会放弃 GIL，每个回调
边界都会在宿主代码运行前把它取回。清理持有 GIL。当前
调度拓扑保持不变。

Context 从 Active 变为 Closing 再变为 Closed。
Closing 拒绝新的进入。进行中的回调保留其元数据，并在延迟拆除
之前完成。Closing 使所有已注册句柄失效，包括函数接收者、
脚本和异常状态。显式关闭和 GC 使用相同的资源清理；GC
不派发页面生命周期事件。

ContextResources 为宿主所有者、回调锚点、包装器和 realm
依赖建立索引。Python 引用通过 tp_traverse 可见。Isolate
协调 V8 进入和待处理清理，而不是强 Python 回调根。
V8 External 值指向原生所有权记录，而非 Python 对象。
弱回调将预分配记录入队；减引用发生在 V8 弱阶段之外。
跨 Isolate 句柄会被拒绝。同 Isolate 转移保守地保留源，
直到任一 Context 关闭。

PythonBoundary 在 Python API 槽位处转换原生 C++ 失败；
CallbackBoundary 在已安装的宿主回调边界处转换失败。
分配失败并不承诺 V8 堆内存 OOM 可以被恢复。
这是进程内嵌入，而非针对恶意代码的安全沙箱。

## 性能与验证

`exec_js` 复用 `ExecutionScope` 的 Context 进入，属性回调在入口完成一次作用域与活跃检查；可迁移 Isolate 的 Locker 存放在作用域内，锁等待仍释放 GIL。短单字节 JS 字符串直接按 Latin-1 解码，CPython 自行选择 ASCII 表示。

一次 `to_python()` 共用一份对象身份表，前 16 个对象放在栈内数组，较大的图使用哈希溢出表。表中保存 V8 Local 句柄，通过句柄比较对象身份，允许 GC 移动对象，保留环与共享引用。该表只存活于本次物化，没有持久结果缓存。枚举得到的私有稠密键数组按数据读取；源对象的 getter、Proxy 检查和属性值访问仍通过可重入且释放 GIL 的路径执行。

当 V8 已捕获 getter/Proxy 异常时，构造 Python 异常前清除转换器的备用错误，避免原始错误被 CPython 包装成 `SystemError`。原始 JS 抛出值的身份仍保留。

`benchmarks/bridge.py` 运行五个子进程，每个包含五个预热批次
和二十个测量批次。JSON 包含原始样本、二进制 SHA-256 和
源码版本。请用相同的 V8/编译器配置运行两份检出。
热路径几何平均包含回调、方法、访问器、宿主对象往返和异常。
Trace、DOM 和页面生命周期单独报告。运行基准时不要
并发进行构建或测试。

```powershell
$env:PYTHONPATH = "$PWD\pyv8;$PWD\pyv8c"    # run from the repository root
python benchmarks/bridge.py --output "$env:TEMP/bridge-current.json"
python benchmarks/lifecycle.py --count 1000 --output "$env:TEMP/lifecycle-current.json"
python benchmarks/plot_results.py --input "$env:TEMP/lifecycle-current.json" --output-prefix "$env:TEMP/lifecycle-current"
python -m pytest -q
```

目标是热路径几何平均耗时至少降低 15%，且单个核心或
生命周期工作负载不出现超过 5% 的稳定回归。跨平台和
sanitizer 结果必须与本地 Windows 测试分开报告；配置了
验证命令并不等于成功运行。`pyv8/tools/validate_linux.sh`
为桥接启用 ASan/UBSan。完整的 V8 覆盖还要求 V8 库
使用匹配的 sanitizer 插桩构建。


## 原生评审修正（2026-10-14）

`JSObject._buffer_view()` 仅导出固定大小的 BackingStore。可调整大小的
ArrayBuffer、可增长的 SharedArrayBuffer 及其视图会抛出 TypeError；
`bytes(value)` 仍可用于复制其当前范围。仅凭 BackingStore 引用无法在
ArrayBuffer.resize() 时锁定已提交内存。固定缓冲区导出仍为只读、
遵守缓冲区标志，并拒绝 PyBUF_WRITABLE。

在已持有某个 Isolate 时进入另一个 Isolate，绝不会等待有争用的
进入锁：而是抛出 RuntimeError。无争用的嵌套、返回已持有的
Isolate 以及普通的顶层等待仍受支持。宿主应在从回调返回后
再派发有争用的跨 Isolate 工作。进入失败时，旧有进入宏也会
在构造 V8 作用域之前停止。

Watchdog 截止时间使用可增长的栈：每个嵌套作用域都有自己的
截止时间条目，并在展开时恰好移除该条目。嵌套超过八层不再
丢弃外层截止时间。超时和异常展开测试在子进程中运行，并
验证后续执行仍能成功。

不带显式 isolate 的 `current_context()` 和 `incumbent_context()`
会先查询本线程的活动 Isolate。在执行之外，它们保留默认的
Isolate 回退。显式的 Isolate 参数仍查询该 Isolate。这可防止
Worker 回调仅仅为了在序列化或可转移对象处理期间发现自己的
Context，就去获取父级无关的 Isolate。

上下文查询只观察调用线程已有的进入。执行之外返回 `None`，不为读取空闲上下文创建
顶层 Locker，也不等待另一个线程退出 JavaScript。同一线程跨 Isolate 嵌套时仍能查询外层
Context；回调内查询另一个被占用的 Isolate 仍拒绝等待，抛出原有的嵌套执行错误。
显式参数的类型和关闭状态校验保留。初始化协议没有改变，首次使用仍可启动 V8。

V8 15.4 的顶层 Locker 会初始化线程状态并重置中断标记。之前的空闲查询因此可能清除
后台优化所需的通知，使短脚本之间的 TurboFan 编译停滞。现在 `current_context()` 与
`incumbent_context()` 共用只读查询实现，先检查已有进入，再读取 V8 上下文；
没有取消超时、强制同步 JIT 或使用 V8 私有编译接口。

`Isolate(thread_bound=True)` 在创建时固定 VM 访问和关闭所属的线程。Page 执行组与 Worker
统一采用此模式，在各自执行器的 owner 线程构造和关闭 isolate。进入时校验线程并保留宿主互斥，
不创建 V8 Locker，避免在调用间反复初始化 V8 线程状态、丢失后台编译中断。
后台编译仍并发执行。`Isolate()` 的跨线程迁移模式使用 Locker 保护实际进入；两种模式可在同线程嵌套。

应在 owner 线程退出前显式关闭线程绑定的 isolate。其他线程的 VM 访问及显式关闭会被拒绝。
当整个 Isolate 的最后一个 Python 引用在异线程释放，且没有正在进入或执行的线程时，析构会使用 V8 Locker 完成回收。
单个 Context 等对象的异线程 GC 清理仍由延迟队列交给 owner，等下次进入或关闭时处理。
线程安全的操作终止和前台通知等待仍可由控制线程发起。终止恢复、跨线程 GC、上下文查询和
混合模式嵌套都有回归覆盖。


## 构建与所有权契约（2026-10-14）

常规构建跟踪项目的 .h/.def 和源文件内容，而不只是 .cpp 时间戳。
`build/pyv8-extension.json` 将输入和编译器标识与已构建二进制的
SHA256 关联。仅头文件的改动无需强制即可使产物失效。

需要等待的 Context/句柄工作使用 ExecutionScope。import_handle 在获取
Isolate 后重新校验目标和源句柄。Closed 句柄绝不重建依赖记录。
进入计数器和待处理队列是私有的；Inspector 和关闭注册自行持有
其引用并在本地回滚。


## 可维护性契约（2026-10-14）

构建在比较输入之前先解析并初始化实际使用的编译器。工具路径、
可执行文件摘要、SDK include/library 路径和编译器选项都参与
指纹；自动选择的工具链发生变化时必须重新构建扩展。构建
准备代码也是输入之一。布局检查通过 AST 读取字面的 Extension
include 根目录，并针对包含文件和这些根目录解析引号 include，
而不是匹配基本文件名。

handler 存储和 IdlSpec 构造函数元数据展开 handler_table.def。
运行时契约测试要求每个 handler 具有声明的 stub 关键字、
None 默认值、不可变替换，以及一致的回调校验。BufferRange
持有其 BackingStore 并描述当前字节偏移量/长度；复制和
固定存储导出共享该计算。可调整大小的存储继续拒绝零拷贝
导出。范围不会跨 JS 执行缓存。

DeadlineScope 持有一张不透明票据；Context 手动截止时间有
单独的所有者。clear_deadline 仅移除该 Context 最新的手动
票据，且在不存在时为空操作。逻辑关闭会释放该 Context 的
所有手动票据，即使原生绑定元数据要等到外层进入返回后
才能释放。清理可以撤回该截止时间尚未投递的空闲中止，
但不能撤回显式的 terminate() 或堆终止。空闲到期会在
下一次跨界投递一次，除非其所有者先清除/关闭它。

终止状态转换区分请求中、V8 下发、待投递、活动展开、
完成和恢复。后续的进入不能在 TerminateExecution 实际返回
之前取消中止；嵌套展开不能宣称外层跨界已完成。DeadlineScope
的 start/stop 为 void 且幂等；分配失败通过现有的原生异常
边界传播。这些所有权变更并不宣称能从每一种 V8 致命 OOM
场景中恢复。

Context(timeout=None) 选择零；exec_js(timeout=None) 继承 Context
设置。非有限或无法表示的截止时间抛出 ValueError。无效的
参数解析在触及 V8 值之前返回 TypeError。实际的关键字是
exec_js(program=...)，现已在 stub 中正确命名；调用方应把
不受支持的 script= 关键字替换为 program=，或改用位置参数。
