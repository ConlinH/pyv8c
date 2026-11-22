# 运行跟踪

**简体中文** | [English](../en/pyv8c/tracing.md)

[文档索引](../README.md)

## 启用跟踪

Trace 观察绑定操作的参数、结果和异常；请求拦截器则会改变资源结果，两者独立。

```python
from pyv8c.async_api import Browser, run

async def main():
    async with Browser(real_network=False, traceable=True) as browser:
        page = browser.new_profile().new_page()
        page.trace = True
        page.evaluate("document.createElement('div').id")
        page.trace = False
run(main())
```

`trace=True` 初始开启并允许跟踪；`traceable=True` 允许之后开启。两者都关闭时，不能临时将页面 trace 设为 True。`page.trace` 只接受 bool，切换通过 owner 串行执行，不重建页面对象。

Profile 和 Page 可以设置各自初始 trace，Browser 上的配置不广播改变已有页面。iframe 及 Worker 的初始跟踪由各自创建路径决定；修改父页面开关不能当作全页面树同步开关。

## 按脚本 URL 筛选

`trace_scripts` 接受一个 Glob 模式字符串或非空模式列表。模式对**完整的脚本 URL**做区分大小写的匹配，`*` 匹配任意长度、`?` 匹配单个字符；查询参数参与匹配。筛选发生在原生 trace 入口，未匹配的操作不会转换 trace 参数或进入回调。

```python
from pyv8c.async_api import Browser, run

async def main():
    async with Browser(trace=True, trace_scripts='https://cdn.example.test/sdk/p.js*') as browser:
        async with Browser(trace=True, trace_scripts=['https://cdn.example.test/sdk/p.js?*', 'https://cdn.example.test/sdk/ips.js?*']) as browser:
            pass
run(main())
```

`new_profile(trace_scripts=...)` 和 `new_page(trace_scripts=...)` 可覆盖上层配置；省略时继承，显式传入 `None` 时清除上层筛选并跟踪所有脚本。iframe 继承父页面的模式，Worker 继承创建它的页面的模式；导航后的新 Realm 保留本页面配置。`page.trace` 仍只负责开启或关闭跟踪。

归属依据是操作发生时 JS 栈中最近的具名脚本，不是发出资源请求的 URL 或 TOP/FRAME/Worker 标签。目标脚本注册的定时器、事件和 Promise 回调在执行时也会被筛中。内联脚本目前使用文档 URL 作为编译文件名；没有可归属脚本名的宿主操作在启用筛选后不记录。未设置 `trace_scripts` 时维持原有 trace 行为。

## 事件回调

默认使用 Loguru。自定义 trace_callback 接收八个参数：

```python
from pyv8c.pyv8 import format_trace

def observe(event, receiver, name, arguments, value, error, is_construct, ctx_type):
    message = format_trace(event, receiver, name, arguments, value, error, is_construct, ctx_type)
    if message is not None:
        print(message)
```

| 参数 | 内容 |
| --- | --- |
| event | 例如 get、set、method、construct、has 或 descriptor；具体取决于实际进入的绑定操作 |
| receiver | 宿主接收对象，构造事件为类，无法解析时为 None |
| name | 属性、方法或构造器名称，索引可为整数 |
| arguments | 方法/构造参数元组，属性事件为 None |
| value | 操作结果或 setter 输入，失败读取/调用为 pyv8.Undefined |
| error | 成功为 pyv8.Undefined，失败为异常的 Python 表示 |
| is_construct | 是否通过 new 调用 |
| ctx_type | Context 类型标签，如 Window 或 Worker |

回调在事件所属线程同步执行。多 Realm 可共用 sink，共享状态需自行同步；不要持锁反向等待另一 owner。回调返回值不改变业务结果，观察器自身异常与业务异常隔离，执行终止时不再派发观察回调。

## 观测边界

Trace 主要覆盖 IDL getter/setter、方法、构造器及实际处理操作的 named/indexed handler。不保证记录纯 JS 自定义属性、常量、普通方法属性读取或未进入绑定回调的拒绝操作。

基本值按值传递，复杂 JS 对象使用活引用，不自动枚举为 Python 数据。默认 format_trace 避免读取属性或调用页面覆写的 toString；对象显示为 `<JSObject>...` 不是值缺失。set 失败时仍保留输入值，不为确认写入再次读取。

sink 内访问 JSObject、调用 evaluate 或修改 receiver 会产生真实副作用。需要被动观察时只使用已有参数及 formatter；持久保存活引用还会延长对象寿命，应及时释放，Context 关闭后不得继续访问。

## 定位问题

先采集未注入探针的加载结果，再针对最早因果分歧开启观察。单条 undefined、异常或缺失请求只是线索，应结合 Realm、时序及 Chrome 对照判断。trace、stub_logging 和 DevTools 的启用状态均应作为实验条件记录。

未实现成员日志由 `stub_logging` 控制：off 不记录，on 每个成员记录一次，verbose 包含更多占位处理信息。成员缺口日志不等于实际执行一定失败，也不代表占位行为正确。
