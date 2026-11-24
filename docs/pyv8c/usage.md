# 使用指南

Browser 使用同线程与协作式等待；完整契约见[协作式 Browser](cooperative-runtime.md)。

**简体中文** | [English](../en/pyv8c/usage.md)

[文档索引](../README.md)

## 创建与关闭

`Browser` 管理公共配置，`Profile` 管理会话状态，`Page` 管理页面。不同 Profile 的 Cookie 和内存状态独立；同一 Profile 可以创建多个页面。


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
        profile = browser.new_profile()
        page = profile.new_page()
        await page.set_content("<p id='text'>内容</p>", url='https://example.test/')
        assert page.evaluate("document.querySelector('#text').textContent") == '内容'
asyncio.run(main())
```

`new_page()` 默认地址为 `about:blank`。加载主文档使用 `goto()`，不要将创建 Page 与完成网络导航混为一谈。

`await page.close()`、`await profile.close()`、`await browser.close()` 逐级释放所有者资源，正常关闭可重复调用。独立资源清理失败会报告异常，其他资源仍尽力释放；不能将异常当作成功关闭。关闭子页面不会停止仍被其他页面使用的执行组。

公开关闭操作属于 owner 线程；活跃 JS 的独立停止信号由内部控制通道处理。后台任务因匹配的关闭票据终止时不报告宿主故障；正在等待同步操作的宿主仍会收到 `pyv8.JavaScriptTerminated`。该异常继承 `BaseException`，不属于普通 JS 异常。需要分类处理时应显式捕获，真实超时和未知终止不能统一忽略。

## 保存并复用登录状态

为 `new_profile()` 指定目录后，pyv8c 会恢复 Cookie 和 HTTP(S) 源的 `localStorage`。同一个目录一次只能由一个 Profile 打开。为不同账号使用各自的目录。

```python
from pathlib import Path
from pyv8c.sync_api import Browser

with Browser() as browser:
    with browser.new_profile(
        user_data_dir=Path("userdata/account-a"),
        persist_session_cookies=True,
    ) as profile:
        page = profile.new_page()
        page.goto("https://example.com/")
        # 在这里按站点要求执行登录。
        profile.flush()
```

下一次用同一目录打开 Profile，页面首次请求就能使用仍有效且符合发送条件的 Cookie。默认只保存有过期时间的 Cookie，示例显式开启了会话 Cookie 恢复。异步版同样直接调用 `new_profile()`，需要立即保存时用 `await profile.flush()`。运行期间会合并写入，正常关闭会提交剩余变更。同步应用的后台保存随 Browser 事件循环推进，宿主线程空闲时可使用 `flush()` 确保状态落盘。

SQLite 文件中的 Cookie 和站点存储可能含有登录凭据。请放在当前用户的私有目录中，避免提交到版本控制或分享数据库文件。保存范围为 Cookie 和 HTTP(S) `localStorage`，`sessionStorage` 仍随页面会话结束。

## 页面 API

| 调用 | 契约 |
| --- | --- |
| `set_content(html, url="about:blank")` | 注入 HTML 并等待 wait_until（默认 load）；commit 可观察未完成的解析 |
| `goto(url, timeout=30, wait_until="load")` | 导航并等待 `commit`、`domcontentloaded` 或 `load`；`None` 按提交阶段处理 |
| `evaluate(script, filename=None, timeout=30)` | 执行源码并物化可返回的数据，不自动等待 Promise |
| `evaluate_handle(script, filename=None)` | 返回基本值或活 JS 句柄，不进行数据快照转换 |
| `wait_for(expression, timeout=30)` | 等待 JS 条件成立；条件应无副作用 |
| `wait_for_load_state(state="load", timeout=30)` | 等待当前文档代次的加载状态，文档替换或关闭会失败 |
| `run_until_idle(timeout=1.0)` | 等待目标 Agent 可运行工作及检查点排空；成功返回 True，超时返回 False |
| `sleep(seconds)` | 等待固定时长并持续驱动页面事件循环；不执行 JS、不等待任何条件 |
| `dispatch_event(event_type, ...)` | 向指定窗口、文档或元素派发宿主构造的事件 |
| `set_visibility(hidden)` | 更新可见性并驱动相关生命周期行为 |

`goto(..., wait_until="domcontentloaded")` 可能在 `load` 监听器仍运行时返回。网络请求完成、DOM 就绪和应用就绪是不同条件，应分别等待。加载流程被终止时，等待 `load` 会失败，不把已经开始派发当作加载成功。

```python
await page.set_content('<script>globalThis.ready = false; setTimeout(() => ready = true, 0)</script>')
await page.wait_for_load_state('load', timeout=2)
await page.wait_for('ready === true', timeout=2)
```

`run_until_idle(timeout=0)` 只查询状态。远期 timer、在途网络、长连接和存活 Worker 不直接阻止空闲；不要用空闲等待代替业务条件等待。owner 线程内部不得同步等待未完成的导航或网络操作。

`page.sleep(seconds)` 与宿主的 `time.sleep()` 不同：它让页面的事件循环继续运转，timer、网络回调和 CDP 驱动等可运行任务会在等待期间推进。它不执行任何 JS，因此不参与条件判断，也不会抛出 `JavaScriptTerminated`。给页面一点时间推进时用 `sleep`，等待某个业务条件成立时仍用 `wait_for` / `wait_for_selector`，不要把 `sleep` 当成条件等待的替代品。异步 API 中它等价于 `await asyncio.sleep(seconds)`，提供该方法只为保持同步/异步接口一致。

## 返回值与句柄

`evaluate()` 将对象和数组转换为可物化的 Python 数据。需要 DOM、函数等对象身份时，使用 `evaluate_handle()`；操作必须回到所属执行组的 owner 线程：

```python
handle = page.evaluate_handle("({answer: 42})")
snapshot = handle.to_python()
assert snapshot == {"answer": 42}
```

`page.executor` 属于内部执行设施，只有需要原生句柄操作时才使用。不要从其他线程直接读取句柄，也不要在不相关页面间传递活对象。导航替换 Context 后，旧句柄不再是新文档的对象；原 Context 最终关闭后句柄失效。

## 环境配置

`BrowserEnvironment` 使用扁平键名，构建后不可变。未显式设置的值来自随包 Windows 环境档案及派生规则。省略 `environment` 时 `Browser` 整份使用这份 Windows 档案。传入档案路径则整份使用该文件，例如 `Browser(environment=profile_path("darwin"))`。映射和 `BrowserEnvironment` 仍覆盖 Windows 底稿。`new_page(environment=...)` 可覆盖页面环境。

```python
from pyv8c import BrowserEnvironment

environment = BrowserEnvironment({
    "languages": ["zh-CN", "zh"],
    "viewport.width": 1280,
    "viewport.height": 720,
    "viewport.device_scale_factor": 1,
})
values = environment.resolved()
assert values["viewport.width"] == 1280
```

常用配置包括 `user_agent`、`platform`、`language`、`languages`、`timezone`、`webdriver`、`hardware_concurrency`、`device_memory`，以及 `viewport.*`、`screen.*`、`client_hints.*`、`media.*`、`permissions.*` 等。不是任意字符串都有效，未知键和不合法值会被校验。

`timezone` 使用 IANA 时区名，`language` 使用 BCP 47 语言标签。每个顶层页面和每个 Worker 各自持有一份；后创建的页面或 Worker 保持自己的时区和语言。iframe 使用所属页面的时区和语言。未指定区域的 `Intl` 采用 `language`。`Date()` 的星期和月份保持英文缩写，括号中的长时区名随语言变化。别名会规范成规范名，例如 `US/Eastern` 会变成 `America/New_York`。空字符串以及无法识别的时区或语言，在创建页面或 Worker 时抛出 `ValueError`。

`to_dict()` 返回显式配置，`resolved()` 返回解析后的完整值；`overlay()` 创建覆盖后的新环境，`from_file()`、`to_file()` 用于 JSON 文件。修改环境值不会自动提供真实硬件、权限或媒体解码能力。

`form_controls.submit_label` 和 `form_controls.reset_label` 分别配置提交与重置按钮未显式指定 `value` 时的浏览器界面文字，用于计算按钮尺寸。这些文字由浏览器界面语言决定，可以不同于 `navigator.language`；随包档案使用本机 Chrome 的中文标签。它们不会改变输入元素的 `value` 属性。

`form_controls.file_button_label` 和 `form_controls.file_empty_label` 分别用于估算文件选择控件的按钮及未选择文件时的提示文字尺寸，也应按浏览器界面语言配置。

`form_controls.time_font_family` 指定时间控件默认 `monospace` 字体使用的实际字体家族，供内部字段尺寸计算；页面显式设置的其他字体不受该项影响。
当前时间控件按 24 小时制估算内部字段尺寸，其他界面格式、自定义字体和控件伪元素样式尚不保证与 Chrome 完全一致。

## 浏览器选项

| 选项 | 默认值及含义 |
| --- | --- |
| `real_network` | True；是否装配真实网络引擎 |
| `http_engine` / `ws_engine` | `curl_cffi` / `websockets`；引擎名、实例、工厂或 None。HTTP 可选 `cronet`，WebSocket 只有 `websockets` 一个内建名字 |
| `http_options` / `ws_options` | 各引擎配置，创建 Profile 时校验 |
| `proxy` | None；未设置时直连，Profile 可覆盖 |
| `default_headers` | 宿主 `page.net` 请求的默认头 |
| `resource_loader` / `resource_interceptor` | None；资源来源和拦截回调 |
| `threaded_loaders` | False；是否将宿主回调放入有界线程池 |
| `cache` / `cache_dir` | True / None；只有指定目录才有持久资源响应缓存 |
| `frame_rate` | 60.0；页面树的虚拟帧时钟频率，必须为有限正数 |
| `trace` / `traceable` / `trace_callback` / `trace_scripts` | False / False / None / None；跟踪配置与脚本 URL 筛选 |
| `expose_unimplemented` | True；是否暴露基线中的未实现接口 |
| `stub_logging` | `off`；也可为 `on` 或 `verbose`，控制未实现成员日志 |
| `interface_overrides` | None；接口成员实验性替代回调 |
| `webgl_backend` | `simulated`；WebGL 后端，可选 `angle`（实验性，见 [ANGLE 实验后端](angle-prototype.md)） |
| `angle_library_dir` | None；ANGLE 的 EGL/GLES 库目录，未指定时由 `comfy-angle` 解析 |
| `idl_path` | None。省略时，包内 Mac 档案路径配对 `idl/idl-158.0.8127.36.zip`（文件缺失则回退）。无参 `Browser()`、映射、`BrowserEnvironment` 和其它档案都读包内 `idl.zip`。显式路径优先。直接 `Idl.load()` 仍读包内 `idl.zip` |
| `baseline_mismatch` | `warn`；实现或 Window 顺序与所选基线不匹配时警告，`error` 为严格校验 |

`new_profile(proxy=..., trace=..., traceable=..., trace_callback=..., trace_scripts=...)` 可设置相应会话选项；`new_page(trace_scripts=...)` 也可覆盖脚本筛选，详见[运行跟踪](tracing.md)。网络与资源回调的线程限制见[资源加载](resource-loading.md)。

`interface_overrides` 格式为 `{"接口名": {"成员名": 回调}}`，属性可使用 `{"get": 回调, "set": 回调}`。实例方法接收 `(receiver, *args)`，静态方法接收 `(*args)`，属性读取接收 `(receiver)`，写入接收 `(receiver, value)`。未知成员、非法槽位和只读属性的写入覆盖会被拒绝；不覆盖构造器、常量或 named/indexed handler。正式修复应进入所属实现层。

## 网络调用

`await page.net.get()`、`await page.net.request()` 等都是 asyncio 宿主请求，使用 Profile 网络后端和 Cookie；它们不执行页面 fetch，也不经过完整页面资源策略。取消等待会先尝试中断在途的请求协程，再停止结果观察；传输层收到 `RequestHandle.cancel()` 后负责清理并结算请求，Page 关闭仍按在途计数等待其结算，预算耗尽抛 `HostStateError` 并允许重试。

`await page.net.fetch()`、`await page.net.xhr()` 调用页面当前的 Fetch/XMLHttpRequest，实现可能已被页面替换；结果为宿主响应快照，适用页面资源管线和相应同源规则。不要把两组调用当作可互换入口。

## 事件与输入

```python
await page.set_content("<button id='go'>继续</button>")
page.evaluate("document.querySelector('#go').addEventListener('click', () => globalThis.clicked = true)")
await page.click('#go')
await page.wait_for('globalThis.clicked === true', timeout=1)
```

宿主支持 `click()`、`dblclick()`、`hover()` 和 `page.mouse`。输入行为基于当前布局与命中测试；几何和真实 Chrome 渲染仍可能有差异。`dispatch_event()` 提供明确的事件派发，不等同于完整输入动作序列。

## 穿透 Shadow DOM 与 iframe

`query_selector()`、`click()`、`dblclick()`、`hover()` 和 `wait_for_selector()` 都接受 `deep` 关键字参数。默认 `deep=False` 只在当前文档的 light DOM 查询，与页面 `document.querySelector()` 行为一致。`deep=True` 由宿主按以下顺序搜索，命中第一个即返回：

1. 当前文档的 light DOM；
2. 按 tree order 逐个 host 的 shadow tree，包括 `mode='closed'`（页面脚本无法再取得该 root）；
3. 按 DOM 中 iframe 的出现顺序递归每个子 frame。

多个 frame 都有匹配时返回 tree order 最靠前的那个。这是宿主特权：`deep=True` 能到达页面 JS 看不到的 closed shadow 和跨源 frame，不对应任何页面 API。

```python
node = page.query_selector('#widget', deep=True)      # 可能在 shadow 或 frame 内
await page.click('#widget', deep=True)                # 由该节点所属 Page 的鼠标派发
node = await page.wait_for_selector('#late', deep=True, timeout=3)
```

`click()` / `dblclick()` / `hover()` 会把输入交给节点所属 Page 的鼠标，跨 frame 无需手工切换。返回的节点句柄属于其自身 realm，后续操作仍须在该 Page 的线程/执行器内进行。

选择器组合子只在单个查询 root 内匹配，不跨越 shadow 边界：`#host #inner` 不会命中 host 的 shadow 内的 `#inner`，而同一 shadow tree 内的 `div.wrap > #inner` 正常。选择器引擎未实现 `:host`、`::slotted` 等 shadow 作用域伪类。`wait_for_selector()` 用同一套查询反复轮询直到命中或超时，超时规则与 `wait_for()` 一致。

应用侧异常、宿主故障、网络失败和执行终止是不同结果。排查时先查看失败阶段，再选择[运行跟踪](tracing.md)、[调试器](devtools.md)或本地 Chrome 对照，不通过延长固定休眠掩盖问题。
