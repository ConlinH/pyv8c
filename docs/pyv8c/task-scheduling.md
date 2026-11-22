# 任务调度

Browser 使用同线程与协作式等待；完整契约见[协作式 Browser](cooperative-runtime.md)。

**简体中文** | [English](../en/pyv8c/task-scheduling.md)

[文档索引](../README.md)

## 执行所有权

独立 `new_page()` 创建独立 WindowExecutionGroup，拥有各自的 Isolate、Agent 和帧时钟；其 owner 线程是启动 Browser 的应用线程，与其他页面共享，只有 Worker 使用独立线程。iframe 使用同一执行组；同有效 origin 的 Realm 共享 Agent，其他 Agent 独立排队但仍在该 owner 线程运行。opaque origin 按身份 token 区分，初始 about:blank、srcdoc 和 Blob 的来源按创建关系继承。

每个 Agent 持有任务源队列与显式 V8 微任务队列，Dedicated Worker 使用独立线程、Isolate、Agent 和事件循环。同步 iframe 访问无需跨线程等待；不相关执行组之间不允许直接传递活 JS 对象。

## 任务源与执行入口

任务记录包含来源、所属 Realm/文档、代次、顺序和取消信息。任务源包括 parser、navigation、networking、timer、posted-message、user-interaction、DOM manipulation、media-element 和 embedder。

OfflineAudioContext 的完成通知使用 media-element 来源，避免已完成的渲染被无关 DOM 任务积压。该任务仍受所属文档代次与关闭状态约束；complete 事件先于渲染 Promise 的兑现回调。

普通宿主命令进入 embedder 来源，不无限优先清空。调度公平选择可运行来源，并保持来源内要求的 FIFO；parser 阻塞只影响对应文档。关闭、终止和 Inspector 唤醒使用受限控制入口，不能借该入口执行任意页面 JS。

跨来源不存在一条应被固定的 Chrome 全序，测试约束必要因果顺序和不饥饿。当前不实现完整加载提级、后台预算或动态输入优先级。

owner 从一次任务扫描开始记录唤醒序号。投递方先发布任务或期限，再增加序号并通知条件变量；
owner 只有在序号未变化时才原子地进入等待。空闲和有限期限使用同一等待协议，不轮询，也不启动额外 asyncio loop。

页面命令直接作为托管任务的回调记录，由 Agent 入队后通知 owner；执行器不追加同一次投递的唤醒。
独立控制队列仍自行通知。任务扫描确认空闲后只重新读取期限，并在条件锁内校验扫描前的序号，
不重复扫描所有任务、前台任务及渲染来源。扫描期间的新投递通过序号保留，到期 timer 通过期限重新驱动。
命令登记与路由引用设置共用提交锁，取消、完成和关闭均清理路由引用；取消锁与当前 Agent 锁相同时，
票据校验复用已持有的锁，迁移票据仍获取其原锁。来源轮转、操作预算和微任务检查点规则不变。

同步命令使用单次完成记录；排队超时阻止执行，已开始的操作保留原生终止与迟到结果日志。
结果在预算清理后发布。脚本拥有其微任务检查点；返回对象先完成脚本检查点再复制，
复制过程中执行的 getter 所产生的任务在返回前处理，原始值无需第二次检查点。

迟到日志的对象格式化或日志输出失败不会关闭执行器；执行终止信号仍向外传播。
脚本入口的关闭状态检查或 DevTools 前置钩子失败时，也完成所属检查点，避免 Agent 残留为忙碌状态。
前置钩子失败后不调用后置钩子，保留原始错误；执行终止时仍遵守不再运行页面 JS 的规则。

启用 `threaded_loaders` 时，同步资源加载等待同时受请求期限、当前窗口操作预算和停止请求约束。
后台 loader 无法强制中断，但其迟到完成不会继续占用已退出等待的 owner。宿主 HTTP Future 在
页面关闭或文档替换时以 `HostStateError` 结束；显式取消和迟到结果不会再次完成同一个 Future。
导航成功替换 Realm 后，新 Promise 使用新上下文，已创建的 resolver 仍保留原上下文。

任务取消在锁内标记状态并摘除引用，在调度锁和文档路由锁退出后回收 callback/context。
文档失效、队列关闭及无效任务筛选遵守同一规则，允许析构和弱引用回调重新进入执行器。

## 微任务与拒绝通知

Promise、queueMicrotask 和 MutationObserver 使用所属 Agent 的显式微任务队列。普通属性读取、对象转换和嵌套跨 Realm 调用不应意外启动检查点；脚本与回调清理在 JS 栈条件允许时检查，任务结束兜底，并防止重入。

检查点排空至稳定，不用人为微任务配额改变顺序。无限微任务由操作预算和终止机制处理。Promise 拒绝在检查点收集候选，再通过任务通知 `unhandledrejection`、`rejectionhandled`，不在任意属性调用结束时直接派发。

导航代次失效只取消相应文档的操作，不能清空整个 Agent 队列并误伤兄弟 Realm。

## 网络、导航与等待

外部网络完成投递所属 networking 任务；返回 owner 后的响应转换、跳转和内部 continuation 可在该任务内继续。缓存、本地 URL 和 inline loader 的异步最终结果同样通过队列交付，不因当前在 owner 上就同步调用最终用户回调。

导航不在 owner 阻塞等待主文档网络。`await page.goto()` 协作等待导航完成，owner 可继续处理可运行工作；owner 内部不能同步等待尚未完成的操作，也不能嵌套驱动事件循环完成等待。

| 等待入口 | 判断条件 |
| --- | --- |
| `goto(..., wait_until="commit")` | 当前导航提交 |
| `wait_for_load_state("domcontentloaded")` | 目标文档的 DOMContentLoaded 状态 |
| `wait_for_load_state("load")` | 加载完成流程正常结束 |
| `wait_for(expression)` | 只读 JS 条件成立 |
| `run_until_idle(timeout)` | 目标 Agent 可运行任务排空并完成检查点 |

远期 timer、在途网络、长连接和 Worker 存活不直接阻止可运行队列空闲。`timeout=0` 的空闲调用只查询当前状态；网络或应用就绪必须使用对应等待入口。

## 定时器与渲染机会

定时器堆管理截止时间，到期后进入 timer 来源。取消的堆顶条目必须跳过，不能遮挡后续到期工作。定时器具有嵌套层级和相应最小延迟规则；interval 不是固定最低 1ms 的独立模型。

rAF 属于独立渲染机会，页面树共享帧时钟，默认虚拟 60Hz，可用 Browser 的 `frame_rate` 设置。同一帧使用统一时间基准，回调中新增或取消的 rAF 按当前批次规则处理，隐藏状态影响帧机会。Worker 有独立渲染调度设施，不与页面共享线程。

虚拟帧时钟不代表真实屏幕刷新、绘制或完整 Chromium 渲染流水线。

## 预算、取消与关闭

宿主操作使用提交时计算的绝对截止时间，排队、执行及检查点共享预算。`evaluate(timeout=30)` 默认有限预算；执行器的普通调用默认预算由 `PYV8C_CALL_TIMEOUT` 控制，未设置时为 60 秒，非正配置表示不设置该默认期限。

原生操作用身份票据绑定 watchdog。排队任务可取消；已经开始的工作不能承诺回滚副作用。Isolate 终止作用于整个活跃调用栈，不保证只停止跨 Realm 栈中的一个 Context。纯 Python 阻塞不能靠 V8 watchdog 中断，应移出 owner。

执行终止向外展开时，事件状态和资源登记仍需复位，但不得再运行页面 JS、错误事件或微任务检查点。只有原生接受且匹配当前票据的关闭请求才能被归为预期取消；其他终止仍报告失败。

load 开始与完成分别记录。加载事件中断后，不继续发送后续 pageshow、pagereveal 或成功回调，load 等待者收到失败；文档替换重置旧状态。每次调用页面代码后检查关闭、文档代次和身份。

Inspector 暂停不消耗 JS 执行预算，宿主等待期限仍有效。关闭先唤醒暂停/启动门，再在 owner 完成释放。窗口关闭、Worker 终止和资源过期分别处理，不混同为清空共享队列。
