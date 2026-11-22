# pyv8 使用指南

**简体中文** | [English](../en/pyv8/user-guide.md)

本文档对应 pyv8c 0.2.0 中的内置 V8 引擎。ABI3 目标为启用 GIL 的 CPython 3.12 至 3.15、V8 15.4.68；3.15 已在 Windows 进行部分验证。

桥接内核重构后，JS 对象默认返回句柄，需要 list/dict 时显式调用
`result.to_python()`。接口、所有权和验证说明见 [桥接内核文档](bridge-kernel.md)。

pyv8 的主要目标不是替代浏览器，而是提供一组足够底层的 Python/V8 桥接 API，
用于构造接近 Chromium 行为的原型对象、构造器、属性描述符和 property interceptor。

## 目录

- [安装与构建](#安装与构建)
- [Context 与脚本执行](#context-与脚本执行)
- [类型转换](#类型转换)
- [JavaScript 对象与函数](#javascript-对象与函数)
- [Chromium-like 类与原型](#chromium-like-类与原型)
- [Property interceptor](#property-interceptor)
- [Cross-origin 与 trace](#cross-origin-与-trace)
- [TypedArray 与 Promise](#typedarray-与-promise)
- [异常与超时](#异常与超时)
- [多 Isolate 与生命周期](#多-isolate-与生命周期)
- [DevTools 调试](#devtools-调试)
- [API 速查](#api-速查)
- [测试](#测试)
- [常见问题](#常见问题)

## 安装与构建

### Python 和平台要求

- CPython 3.12 至 3.15（3.15 目前仅在 Windows 完成部分验证）
- 使用 GIL 的标准 CPython 构建
- 支持 C++20 的编译器
- 与目标平台和 CPU 架构匹配的 V8 静态库
- 与该 V8 构建匹配的 `icudtl.dat`

Windows 是当前主要开发和完整测试平台。Windows 源码构建建议使用 Visual Studio 2022
和 Windows 10/11 SDK。Linux/macOS 构建分支已经包含在构建脚本中，但仍应在目标平台
验证编译器、V8 产物和 wheel。

### 安装预编译 wheel

```bash
python -m pip install path/to/pyv8c-0.2.0-cp312-abi3-win_amd64.whl
```

`cp312-abi3` 表示同一扩展可以被启用 GIL 的 CPython 3.12 至 3.15 加载。ABI3 不会让二进制跨
操作系统或 CPU 架构使用，例如 Windows 的 `.pyd` 不能在 Linux 中加载。

### 准备构建工具

以下源码构建命令均从仓库根目录执行。

```bash
python -m pip install --upgrade pip setuptools wheel build tox
```

Windows 下还需要：

- Visual Studio 的“使用 C++ 的桌面开发”工作负载
- Windows 10/11 SDK
- Git
- 足够的磁盘空间和内存用于 V8 编译

### 选择并编译 V8

查看每个发布分支最新的正式 tag：

```bash
python setup.py list_v8_versions --limit=15
```

交互选择版本：

```bash
python setup.py build_v8 --select
```

明确指定版本：

```bash
python setup.py build_v8 --v8-version=15.4.68 --v8-jobs=8
```

选择结果会写入本地 `.pyv8-version`。后续 `build_ext` 和 wheel 构建会复用该版本；
删除 `.pyv8-version` 后恢复使用仓库中的 `V8_VERSION`。

相同 V8 tag、CPU 和 GN 参数会复用 `.pyv8-build` 缓存。需要完整重建时使用：

```bash
python setup.py build_v8 --v8-version=15.4.68 --force
```

### 网络和代理

显式指定代理：

```bash
python setup.py build_v8 --select --proxy=http://127.0.0.1:7890
```

忽略进程环境和 Git 全局代理，强制直连：

```bash
python setup.py build_v8 --select --no-proxy
python setup.py list_v8_versions --no-proxy
```

也可以使用环境变量：

```powershell
$env:PYV8_PROXY = "http://127.0.0.1:7890"
python setup.py build_v8 --select
```

```powershell
$env:PYV8_NO_PROXY = "1"
python setup.py build_v8 --select
```

### 编译 Python 扩展

在仓库根目录原地编译，生成 `pyv8c/pyv8c/pyv8/_pyv8.pyd`（Windows）或 `_pyv8.abi3.so`（Linux）：

```bash
python setup.py build_ext --inplace
```

V8 或 C++ 源码变化后强制重新编译：

```bash
python setup.py build_ext --inplace --force
```

构建可发布 wheel：

```bash
python -m build --wheel --outdir dist .
```

使用 Python 3.12 构建可以得到 `cp312-abi3` wheel，供启用 GIL 的 Python 3.12 至 3.15 使用。

### 使用已有 V8 产物

已有自行编译的 V8 时，不需要重新下载：

```powershell
$env:PYV8_V8_VERSION = "15.4.68"
$env:PYV8_V8_HOME = "D:\v8-workspace\v8"
$env:PYV8_V8_OUT = "D:\v8-workspace\v8\out\x64.release"
$env:PYV8_ICU_DATA = "D:\v8-workspace\v8\out\x64.release\icudtl.dat"
python -m build --wheel --outdir dist .
```

Linux/macOS 使用相同变量语义：

```bash
PYV8_V8_VERSION=15.4.68 \
PYV8_V8_HOME=/opt/v8 \
PYV8_V8_OUT=/opt/v8/out/x64.release \
PYV8_ICU_DATA=/opt/v8/out/x64.release/icudtl.dat \
python -m build --wheel --outdir dist .
```

`PYV8_V8_OUT` 中必须存在：

- Windows：`obj/v8_monolith.lib`
- Linux/macOS：`obj/libv8_monolith.a`
- `icudtl.dat`

### 构建环境变量

| 变量 | 作用 |
| --- | --- |
| `PYV8_V8_VERSION` | V8 正式数字 tag |
| `PYV8_BUILD_V8` | 设置为 `1` 时下载并构建 V8 |
| `PYV8_FORCE_V8_BUILD` | 设置为 `1` 时忽略 V8 构建缓存 |
| `PYV8_V8_JOBS` | Ninja 并行任务数 |
| `PYV8_BUILD_ROOT` | depot_tools 和版本缓存根目录 |
| `PYV8_DEPOT_TOOLS` | 使用已有 depot_tools 目录 |
| `PYV8_PROXY` | Git、depot_tools 和 CIPD 使用的代理 |
| `PYV8_NO_PROXY` | 设置为 `1` 时强制直连 |
| `PYV8_VS_HOME` | Windows Visual Studio 安装目录 |
| `PYV8_VS_VERSION` | Visual Studio 年份，默认 `2022` |
| `PYV8_WINDOWS_SDK` | Windows SDK 根目录 |
| `PYV8_V8_HOME` | 已有 V8 源码目录 |
| `PYV8_V8_OUT` | 已有 V8 输出目录 |
| `PYV8_ICU_DATA` | 与 V8 静态库匹配的 ICU 数据文件 |
| `PYV8_CPPGC_MICROTASK_QUEUE` | 缺少可识别的 V8 Ninja 元数据时，按实际构建设置为 `1` 或 `0`；与可识别元数据冲突时构建失败 |
| `PYV8_GN_ARGS` | 追加自定义 GN 参数 |
| `PYV8_TARGET_CPU` | 目标 CPU，目前支持 `x64`、`arm64` |

自动下载构建时，检出或同步失败会直接终止，不会忽略脏工作树或回退到未经验证的源码。已有可用产物可通过 `PYV8_V8_HOME` 和 `PYV8_V8_OUT` 显式指定；ABI 开关必须以该产物的构建证据为准，不能根据版本猜测。

#### Windows 2026 构建示例

项目的 V8 构建器会下载 `V8_VERSION` 指定的源码 tag，并使用 depot_tools
同步依赖、生成 GN 配置和执行 Ninja。Windows 上可以显式固定 Visual Studio
和 Windows SDK，避免机器上存在多个工具链时被自动探测到错误版本：

```powershell
$env:PYV8_V8_VERSION = "15.4.68"
$env:PYV8_VS_HOME = "D:\soft\Microsoft\vs2026"
$env:PYV8_VS_VERSION = "2026"
$env:PYV8_WINDOWS_SDK = "D:\Windows Kits\10"
$env:PYV8_BUILD_ROOT = "D:\src\pyv8env\pyv8c\pyv8\.pyv8-build"
$env:DEPOT_TOOLS_WIN_TOOLCHAIN = "0"

python setup.py build_v8
python setup.py build_ext --inplace
```

`PYV8_VS_HOME` 必须指向 Visual Studio 安装根目录（包含 `VC`、`Common7`），；`PYV8_WINDOWS_SDK` 必须指向包含 `Include`、`Lib`
的 Windows SDK 根目录。

如果网络受限，可设置 `PYV8_PROXY` 或 `PYV8_NO_PROXY=1`。构建完成后，V8
产物位于 `.pyv8-build\versions\15.4.68\v8\out\pyv8-x64.release`，其中
`obj\v8_monolith.lib` 和同目录的 `icudtl.dat` 必须来自同一次构建。

### 初始化时机

`from pyv8c import pyv8` 不启动 V8：扩展只注册类型、记下与自己同目录的 `icudtl.dat`，并做两项
廉价校验（free-threaded CPython、非主解释器会被拒绝）。V8 平台与默认 Isolate 在**首次
使用**时才启动——首次 `pyv8.Context(...)`、`pyv8.Isolate()`、`pyv8.Script(...)`、
`pyv8.new(...)`、`pyv8.current_context()`、`pyv8.v8_gc()`、`pyv8.low_memory_notification()`
或 typed array/Promise 的默认 Isolate 回退。此后每次判定只是一次布尔检查。

`pyv8.v8_version()` 与 `pyv8.hidden`/`pyv8.unconstructable` 不启动 V8。

启动内容由 `pyv8.configure(...)` 决定，且只能在首次使用前调用：

```python
from pyv8c import pyv8

pyv8.configure(params=["--expose-gc"], icudtl_path="/path/to/icudtl.dat")
print(pyv8.is_initialized())  # False
context = pyv8.Context(object)  # 到这里才启动
print(pyv8.is_initialized())  # True
```

参数类型错误在 `configure()` 时就报出来。V8 启动后再调用会抛出 `RuntimeError`
（启动参数与 ICU 数据文件无法在启动后更改）。

### ICU 数据约束

构建时会计算 `icudtl.dat` 的 SHA-256，并把结果编译进 `_pyv8`。首次使用 V8 时会在
V8 启动前重新计算文件哈希。如果文件不是构建时使用的那一份，这次调用会抛出
`RuntimeError`，避免不匹配的 ICU 数据在 `Intl` 初始化中造成 native crash 或 OOM；
文件读不到时错误会给出可操作提示，可用 `pyv8.configure(icudtl_path=...)` 指向它。

哈希相同只表示文件内容没有变化。仍应保证 `v8_monolith` 和 `icudtl.dat` 来自同一个
V8/ICU 构建，不要单独替换其中一个文件。

### 堆上限与外部内存

V8 的堆上限默认按物理内存推算，也可以在启动前用 `--max-old-space-size` 指定（见
`pyv8.configure`）；指定了它就是这次运行的堆预算。`_pyv8` 在 isolate 的线程上盯住用量：
越过预算的 60% 先做一次限频回收（把只是垃圾的部分还回来），越过 80% 就在下一个安全点终止
正在执行的脚本。成功终止时，脚本收到 `JavaScriptTerminated`，随后可以释放引用并继续执行；
`Context.heap_terminations` 记录发生过多少次。

检查发生在 API interrupt 安全点及 GC 结束时。终止后可能仍有大对象被引用；此时允许有限的
诊断与清理分配，继续明显增长会再次请求终止。恢复状态按实际退出时的用量记录，回收至
阈值以下后重置，属于当前 Isolate，不与其他实例共享。它不允许绕过恢复上限持续分配。

终止必须早于 V8 自己的堆耗尽：V8 在收集内部耗尽时直接 abort 进程，从里面调
`TerminateExecution` 已经没有机会生效，所以 `near_heap_limit_callback` 只作为最后一道
兜底：请求同一个终止，并一次提供已有的恢复余量，绝对上限不超过 V8 初始 old-space
上限的两倍。V8 可能在回调返回后立即重新检查用量，逐次小幅增长不足以保证退出。
恢复余量不能作为脚本可用的正常预算，也不保证所有分配都可恢复：大块原生分配可能在
下一个安全点前耗尽余量，使 V8 直接终止进程。需要故障隔离的任务必须放在独立子进程中。

桥接层为 JavaScript 持有的 Python 内存（编译过的脚本源码、被 JavaScript 引用的宿主对象）
V8 看不见，因此由 `_pyv8` 报给它，让它据此决定何时回收：

```python
isolate = pyv8.Isolate()
context = isolate.context(object)
print(isolate.external_memory)  # 已报给 V8 的字节数
```

该数值随宿主对象被包装而增长、随其释放而回落，isolate 关闭后为 0。脚本源码按字符数计量，
宿主对象按固定成本估算（稳定 ABI 无法查询任意对象的大小），因此它表达的是压力规模，不是
内存核算。

## Context 与脚本执行

### 创建 Context

```python
from pyv8c import pyv8


class Window:
    __v8_idl_spec__ = pyv8.IdlSpec(
        global_this=True,
    )


context = pyv8.Context(
    Window,
    trace=False,
    ctx_type="Top",
    timeout=1.0,
)
```

构造参数：

| 参数 | 说明 |
| --- | --- |
| `global_this` | Python 类或实例，用作 JavaScript 全局对象的底层实现 |
| `trace` | 是否立即开启原生绑定观察 |
| `traceable` | 初始关闭时是否配置后续开启观察的能力 |
| `trace_callback` | Context 级事件回调，省略时使用默认日志 |
| `trace_scripts` | 非空脚本 URL Glob 模式元组；仅记录当前 JS 栈来源匹配的绑定事件，省略则记录所有来源 |
| `ctx_type` | 传给 trace 的上下文标签，例如 `Top`、`Iframe` |
| `timeout` | 默认脚本超时秒数，`0` 表示不限制 |
| `isolate` | 指定自定义 `pyv8.Isolate`，省略时使用默认 Isolate |

传入 Python 类时，Context 会无参数实例化该类，因此类构造函数不能要求必填参数。
需要提前准备状态时传入实例：

```python
class Window:
    def __init__(self, origin):
        self.origin = origin


window = Window("https://example.com")
context = pyv8.Context(window)
```

### 执行 JavaScript

```python
result = context.exec_js("20 + 22")
assert result == 42
```

指定脚本文件名有助于异常堆栈和 DevTools 定位：

```python
result = context.exec_js(
    "(new Error('trace')).stack",
    filename="https://example.com/assets/app.js",
)
```

单次执行可以覆盖 Context 默认超时：

```python
context.exec_js("runTask()", timeout=0.5)
```

对字符串程序，编译和执行共用这一次预算，不在编译完成后重新计时。`timeout=0` 覆盖
Context 的默认超时，但不会取消外层 `set_deadline` 等操作预算。某些 V8 编译阶段不能立即
响应中断，因此编译返回时还会核对预算并报告 `JavaScriptTerminated`，不能承诺精确时刻抢占。

### 预编译 Script

```python
script = pyv8.Script(context, "40 + 2", "answer.js")
result = context.exec_js(script)
assert result == 42
```

`Script` 属于创建它的 Isolate，不能交给另一个 Isolate 的 Context 执行。
直接构造 `Script` 使用 Context 默认超时，并继承外层操作预算；编译超时后仍可在同一
Context 中发起后续操作。预编译对象再次执行时，使用该次 `exec_js` 的预算。

### 访问 globalThis

`context.glob` 返回全局对象的 `JSObject` wrapper：

```python
global_object = context.glob
global_object.answer = 42
assert context.exec_js("globalThis.answer") == 42
```

Context 未定义的 Python 属性会自动转发到 JavaScript 全局对象：

```python
context.name = "pyv8"
assert context.exec_js("name") == "pyv8"

context.exec_js("globalThis.version = 2")
assert context.version == 2
```

读取不存在的 Context 属性会抛出 `AttributeError`，而 JavaScript 中仍然得到
`undefined`。

### 暴露 Python 值

位置参数使用对象的 `__name__`：

```python
def add(left, right):
    return left + right


context.expose(add)
assert context.exec_js("add(20, 22)") == 42
```

关键字参数显式指定 JavaScript 名称：

```python
class State:
    pass


state = State()
state.value = 42
context.expose(browserState=state, payload={"ready": True})
```

一次可以暴露多个值：

```python
context.expose(add, State, state=state, config={"debug": False})
```

暴露某个模块的成员要逐个点名，关键字参数就是显式的 JavaScript 名字：

```python
import math

context.expose(sqrt=math.sqrt)
assert context.exec_js("sqrt(81)") == 9
```

### 当前执行上下文

在由 JavaScript 调用的 Python callback 中，可以查询当前 Context：

```python
def inspect_context():
    assert pyv8.current_context() is context
    assert pyv8.incumbent_context() is context
    return 42


context.expose(inspect_context)
context.exec_js("inspect_context()")
```

使用自定义 Isolate 时也可以显式传入 Isolate 查询。

## 类型转换

### Python 到 JavaScript

| Python | JavaScript |
| --- | --- |
| `None` | `undefined` |
| `pyv8.Null` | `null` |
| `pyv8.Undefined` | `undefined` |
| `bool` | `boolean` |
| `int`、`float` | `number` |
| `str` | `string` |
| `bytes` | `ArrayBuffer` |
| `list`、`tuple` | `Array` |
| `dict` | 普通 `Object` |
| Python function/method | JavaScript function |
| Python class | JavaScript constructor/interface object |
| Python instance | 带内部 Python owner 的 JavaScript object |
| `JSObject`、`JSValue` | 原始 V8 值，必须属于当前 Isolate |

Python `None` 不表示 JavaScript `null`：

```python
context.expose(nothing=None, explicit_null=pyv8.Null)
assert context.exec_js("nothing === undefined") is True
assert context.exec_js("explicit_null === null") is True
```

Python 容器的循环引用和共享引用会被保留：

```python
root = []
child = {"root": root}
root.extend((child, child))
context.expose(root=root)

assert context.exec_js("root[0].root === root") is True
assert context.exec_js("root[0] === root[1]") is True
```

JavaScript `number` 无法无损表示任意大的 Python 整数。需要精确大整数时应在 JavaScript
侧使用 `BigInt`，或通过字符串传输。

### JavaScript 到 Python

| JavaScript | Python |
| --- | --- |
| `undefined` | `pyv8.Undefined` |
| `null` | `pyv8.Null` |
| `boolean` | `bool` |
| 32 位整数 | `int` |
| 其他 `number` | `float` |
| `string` | `str` |
| `Array` | `JSObject`；显式 `to_python()` 得到 `list` |
| 普通 object literal | `JSObject`；显式 `to_python()` 得到 `dict` |
| 自定义对象、Date、RegExp | `JSObject` |
| function | `JSFunction` |
| Promise | `JSPromise` |
| TypedArray | `JSTypeArray` |
| BigInt | `JSValue` |

默认转换不枚举属性，不读取 getter 或执行 Proxy trap。显式物化时允许读取属性，
读取失败会传播异常；不能物化的值仍保留句柄。物化保留数组和普通对象的循环、共享引用：

```python
value = context.exec_js(
    "const root = []; const child = {root}; root.push(child, child); root"
).to_python()

assert value[0]["root"] is value
assert value[0] is value[1]
```

显式物化时，稀疏数组中的 hole 会转换为 `pyv8.Undefined`。

## JavaScript 对象与函数

### JSObject

```python
person = context.exec_js(
    """
    new (class Person {
        constructor(name) {
            this.name = name;
        }
        greet() {
            return `Hello, ${this.name}`;
        }
    })("Alice")
    """
)

assert isinstance(person, pyv8.JSObject)
assert person.name == "Alice"
assert person.greet() == "Hello, Alice"

person.name = "Bob"
person["age"] = 18
del person["age"]
```

常用操作：

- `obj.name`、`obj[name]`：读取属性
- `obj.name = value`、`obj[name] = value`：设置属性
- `del obj.name`、`del obj[name]`：删除属性
- `dir(obj)`：枚举属性
- `obj1 == obj2`：比较 V8 对象身份

JSObject 会保留创建它的 Context。释放 Context 前，应先释放不再使用的 wrapper。

### JSFunction 和构造调用

```python
multiply = context.exec_js("(left, right) => left * right")
assert isinstance(multiply, pyv8.JSFunction)
assert multiply(6, 7) == 42
```

调用 JavaScript 构造函数：

```python
Uint8Array = context.exec_js("Uint8Array")
typed_array = pyv8.new(Uint8Array, [1, 2, 3])
```

`pyv8.new()` 的构造器和参数必须能转换到同一个 Isolate。

### Python 对象对应的 JS wrapper

JavaScript 把 Python owner 对象返回给 Python 时，默认恢复为原 Python 对象。需要访问该
对象在 JavaScript 一侧新增的属性时，可以取得 wrapper：

```python
class Payload:
    pass


context.expose(Payload)
payload = context.exec_js("const value = new Payload(); value.extra = 42; value")

wrapper = context.js_obj(payload)
assert wrapper.extra == 42
```

同一 Python 对象在同一个 Context 中保持相同 JavaScript 身份；在不同 Context 或 Isolate
中会创建相互隔离的 wrapper。

## Chromium-like 类与原型

### 构造器基础

```python
from pyv8c import pyv8


class DOMPoint:
    __v8_idl_spec__ = pyv8.IdlSpec(
        constructor_behavior=pyv8.FlagConstructor.kNew,
        has_constructor=1,
        length=2,
    )

    def __init__(self, x=0, y=0, win=None):
        self.x = x
        self.y = y


context.expose(DOMPoint)
point = context.exec_js("new DOMPoint(10, 20)")
```

Python 构造器会额外收到 `win=<当前 Context 的 Python global_this>` 关键字参数。
用于暴露为 JavaScript 构造器的类应接受 `win=None` 或 `**kwargs`。

### 类级元数据

| 属性 | 作用 |
| --- | --- |
| `IdlSpec.length` | JavaScript 构造器的 `length` |
| `IdlSpec.constructor_behavior` | 是否允许 `new Interface()` 和直接调用 |
| `IdlSpec.has_constructor` | 是否在 prototype 上安装 `constructor` |
| `IdlSpec.global_this` | 标记全局 Window-like 类（决定哪个 Python 对象是 JavaScript 全局对象） |
| `IdlSpec.skip_inherit` | 子类建立原型链时跳过该中间基类 |
| `IdlSpec.undetectable` | 使用 V8 undetectable object 行为 |
| `IdlSpec.code_like` | `eval`/`Function` 会把该类的实例字符串化后编译（Blink 的 `TrustedScript` 用这个），其他对象仍原样返回 |
| `IdlSpec.unconstructable` | 构造时抛出“不是构造器”错误 |
| `IdlSpec.iteration_kind` | `IterationKind.none/indexed/pair/set`，选择数据语义 |
| `IdlSpec.array_methods` | indexed 的数组方法掩码：`ArrayProto.iterator`=1、`.iterable`=31 |
| `IdlSpec.iteration_methods` | pair/set 视图掩码，默认 31；iterator=1、entries=2、keys=4、values=8、forEach=16 |
| `IdlSpec.iteration_source` | 宿主工厂；接收 receiver，返回无参数 cursor，每次返回 `(key, value)` 或 None |
| `IdlSpec.immutable` | 将实例或 prototype 的原型标记为 immutable |
| `IdlSpec.call` | 让实例在 JavaScript 中可以作为函数调用 |

`FlagConstructor`：

| 值 | 行为 |
| --- | --- |
| `kDontAll` | 禁止构造和直接调用 |
| `kNew` | 只允许 `new Interface()` |
| `kCall` | 允许函数调用；当前实现也接受 `new` |
| `kAll` | 允许构造和函数调用 |

### Attribute 与 Operation 配置

显式 IDL 成员使用不可变的具名描述；不再接受位置元组。

```python
from pyv8c import pyv8

class Calculator:
    __v8_idl_spec__ = pyv8.IdlSpec(
        attributes=(pyv8.AttributeSpec(name="value", getter="get_value"),),
        methods=(pyv8.MethodSpec(name="add", callback="fn_add", length=2),),
    )

    def get_value(self):
        return 42

    def fn_add(self, left, right):
        return left + right
```

`getter`、`setter` 和 `callback` 接受 Python 回调名或 callable。名称在绑定时解析一次；
显式 IDL 路径不会反射安装未声明的成员。
getter/setter 分别使用 `cross_origin_get_check` / `cross_origin_set_check` 配置跨域规则。

描述对象只读，使用 `spec.replace(**changes)` 创建新配置并共享未变化的成员。
完整字段、注册限制和破坏性迁移说明见 [idl-spec.md](idl-spec.md)。

### 安装位置和 descriptor flags

`FlagLocation`：

| 值 | 安装位置 |
| --- | --- |
| `kInstance` | 每个实例的 own property |
| `kPrototype` | `Interface.prototype` |
| `kInterface` | 构造器/interface object 本身 |

`V8Attribute` 可以按位组合：

| 值 | JavaScript descriptor 含义 |
| --- | --- |
| `kNone` | 默认属性 |
| `ReadOnly` | `writable: false` |
| `DontEnum` | `enumerable: false` |
| `DontDelete` | `configurable: false` |

例如：

```python
flags = (
    pyv8.V8Attribute.ReadOnly
    | pyv8.V8Attribute.DontEnum
    | pyv8.V8Attribute.DontDelete
)
```

### 常量

```python
from pyv8c import pyv8
class ReadyState:
    __v8_idl_spec__ = pyv8.IdlSpec(
        constants_value=(
            pyv8.ConstantSpec(name="UNSENT", value=0),
            pyv8.ConstantSpec(name="OPENED", value=1),
            pyv8.ConstantSpec(name="DONE", value=4),
        ),
    )
```

常量会安装在 interface object 和 prototype 上，并使用只读、不可删除的 descriptor。

### 原型继承

Python 类继承会映射为 JavaScript 构造器和 prototype 继承：

```python
class EventTarget:
    pass


class Node(EventTarget):
    pass


class Element(Node):
    pass


context.expose(Element)
assert context.exec_js("Element.__proto__.name") == "Node"
```

大量生成浏览器接口时，模板会按 Context 和 Isolate 缓存。修改已经安装的 Python 类元数据
不会自动重建旧模板，应创建新 Context 或显式使用新的类对象。

## Property interceptor

### Named property handler

定义下列方法中的任意组合即可安装 named interceptor：

```python
from pyv8c import pyv8
class NamedCollection:
    def __init__(self):
        self.values = {"first": 1}

    __v8_idl_spec__ = pyv8.IdlSpec(name_get="__v8_name_get__", name_set="__v8_name_set__", name_query="__v8_name_query__", name_del="__v8_name_del__", name_enum="__v8_name_enum__")
    def __v8_name_get__(self, name):
        return self.values.get(name)

    def __v8_name_set__(self, name, value):
        self.values[name] = value
        return True

    def __v8_name_query__(self, name):
        if name not in self.values:
            return None
        return pyv8.V8Attribute.kNone

    def __v8_name_del__(self, name):
        return self.values.pop(name, None) is not None

    def __v8_name_enum__(self):
        return list(self.values)
```

返回约定：

| Handler | 返回值 |
| --- | --- |
| `IdlSpec.name_get` | 属性值；`None` 表示未拦截 |
| `IdlSpec.name_set` | 非 `None` 表示已处理 |
| `IdlSpec.name_query` | `V8Attribute` 整数；`None` 表示不存在 |
| `IdlSpec.name_del` | `bool` 删除结果 |
| `IdlSpec.name_enum` | 名称序列；`None` 表示不提供枚举结果 |

### Indexed property handler

```python
from pyv8c import pyv8
class IndexedCollection:
    def __init__(self):
        self.values = [10, 20]

    __v8_idl_spec__ = pyv8.IdlSpec(index_get="__v8_index_get__", index_set="__v8_index_set__", index_query="__v8_index_query__", index_del="__v8_index_del__", index_enum="__v8_index_enum__")
    def __v8_index_get__(self, index):
        if 0 <= index < len(self.values):
            return self.values[index]
        return None

    def __v8_index_set__(self, index, value):
        while len(self.values) <= index:
            self.values.append(None)
        self.values[index] = value
        return True

    def __v8_index_query__(self, index):
        if 0 <= index < len(self.values):
            return pyv8.V8Attribute.kNone
        return None

    def __v8_index_del__(self, index):
        if 0 <= index < len(self.values):
            self.values[index] = None
            return True
        return False

    def __v8_index_enum__(self):
        return len(self.values)
```

`IdlSpec.index_enum` 返回要枚举的连续索引数量，V8 将生成 `0` 到 `length - 1`。

Handler 抛出的 Python 异常会传播到 JavaScript；若未被 JavaScript 捕获，会恢复到 Python
调用方。一次 handler 失败不应破坏后续 Context 执行。只有继承绑定错误族（`pyv8.JsError`
一族）的异常才会进入 JavaScript，其余属于宿主自身（见「Python 异常进入 JavaScript」）。

Setter/deleter 另有一项声明：类同时继承 `pyv8.JsInterceptorFailure` 表示这是一次 legacy
具名/索引属性失败，原生桥按照 `PropertyCallbackInfo::ShouldThrowOnError()` 处理：

- 非严格模式下清除异常，将操作视为已拦截的失败；
- 严格模式下将异常继续抛给 JavaScript；
- 该标记只用于 named/indexed setter 和 deleter，不应加在普通业务异常上。

```python
from pyv8c import pyv8


class LegacyPropertyError(pyv8.JsTypeError, pyv8.JsInterceptorFailure):
    """A legacy write or delete that V8 may leave unraised."""


class LegacyCollection:
    __v8_idl_spec__ = pyv8.IdlSpec(index_set="__v8_index_set__")

    def __v8_index_set__(self, index, value):
        raise LegacyPropertyError(
            f"Failed to set an indexed property [{index}] on "
            "'LegacyCollection': Indexed property setter is not supported."
        )
```

此时 `collection[0] = value` 在非严格模式静默失败，在严格模式产生 JavaScript
`TypeError`。

## Cross-origin 与 trace

### Cross-origin access check

Window-like 对象可以定义访问检查：

```python
from pyv8c import pyv8
class Window:
    __v8_idl_spec__ = pyv8.IdlSpec(cross_origin_check="__v8_cross_origin_check__")
    def __v8_cross_origin_check__(self, accessor):
        # self 是被访问对象，accessor 是发起访问的 Python global object。
        return self.origin == accessor.origin
```

返回 `True` 允许访问，返回 `False` 拒绝访问。

需要在拒绝普通访问时提供受限属性，可以实现独立 cross-origin handler：

- `IdlSpec.cross_origin_name_get`
- `IdlSpec.cross_origin_name_set`
- `IdlSpec.cross_origin_name_query`
- `IdlSpec.cross_origin_name_del`
- `IdlSpec.cross_origin_name_enum`
- `IdlSpec.cross_origin_index_get`
- `IdlSpec.cross_origin_index_set`
- `IdlSpec.cross_origin_index_query`
- `IdlSpec.cross_origin_index_del`
- `IdlSpec.cross_origin_index_enum`

这些方法的参数和返回约定与普通 named/indexed handler 相同。

Attribute 和 operation 配置中的 `FlagCrossOriginCheck` 决定对应 function template 是否接受
跨 origin receiver：

- `kCheck`：执行 receiver/cross-origin 检查
- `kDoNotCheck`：允许任意 receiver

### 补环境 trace

Trace 是原生绑定边界上的 RAII 观察作用域：业务回调仍只执行一次，退出时读取已有参数、返回值和异常。

`trace` 控制当前观察状态；`traceable` 保留为创建时配置观察回调的显式选项，不再表示安装拦截器：

`trace_scripts=("https://example.test/sdk/*.js*",)` 可在原生事件参数转换前按当前执行脚本的完整 URL 筛选。模式支持 `*` 和 `?`，查询参数也参与匹配；异步回调按执行时的 JS 函数来源归属。没有具名脚本栈的宿主操作在启用筛选后不记录。回调参数格式不变。

- `trace=True`：立即观察，之后可以暂停/恢复。
- `trace=False, traceable=True`：初始关闭，之后可以开启。
- 两者均为 `False`：不启用观察能力；之后开启会抛出 RuntimeError。
- `context.trace = False/True` 必须赋 bool，不重建对象；关闭 Context 后不允许切换。

```python
def trace(event, receiver, name, arguments, value, error, is_construct, ctx_type):
    message = pyv8.format_trace(event, receiver, name, arguments, value, error, is_construct, ctx_type)
    if message is not None:
        print(message)

context = pyv8.Context(Window(), traceable=True, trace_callback=trace, ctx_type="Top")
context.trace = True
context.exec_js("/* 观察原生绑定操作 */")
context.trace = False
```

回调契约：

- `event`：`get`、`set`、`method`、`construct`、`has`、`descriptor`。
- `receiver`：Python 对象；构造事件为类；无法解析时为 `None`。
- `name`：JS 属性/方法/构造器名称，索引访问为整数。
- `arguments`：方法与构造事件为 tuple，属性与探查事件为 `None`。
- `value`：读取结果、写入值、调用结果；`has`/`descriptor` 为 bool。get/method/construct 失败时为 `pyv8.Undefined`；set 失败时仍保留写入值，不额外读取写入后的属性。
- `error`：成功时为 `pyv8.Undefined`，失败时为原异常值的 Python 表示。
- `is_construct`：是否通过 `new` 调用。
- `ctx_type`：创建时配置的上下文标记。

`has` 与 `descriptor` 是**探查**事件，不是读取：

- `has`：`in`、`hasOwnProperty` 等存在性查询；`value` 为引擎能否为这个名字解析到属性（沿原型链查找，与 `in` 一致）。
- `descriptor`：`Object.getOwnPropertyDescriptor` 等描述符查询；`value` 为**自身**是否提供该属性（与 `getOwnPropertyDescriptor` 一致）。
- `typeof x` 是一次取值而非探查，只产生一条 `get`。
- 探查不会执行 accessor、不触发 Proxy trap，也永远不会产生 `get`：同一次访问的 getter 日志仍只有一条。

**观察范围**：Python/JS 实现的 IDL 属性和方法、原生构造器、named/indexed 拦截器处理或拒绝的属性访问，以及上述探查。
业务 handler 返回未处理时，原始 V8 查找继续，本层同时按引擎的结果上报该次访问：能无副作用取到真实数据属性的读写都带上真实值，
因此原生全局（`window.eval`、`JSON`、`Object`）和脚本挂的 expando（`element.extra = 1`）都会产生事件。
已声明成员不在拦截器这里上报，它们由各自的 accessor 和调用上报，同一次访问不会出现两条日志。
读取时 accessor 属性和不存在的名字不上报：取值要么需要再次执行 JS，要么只能伪造，宁可少一条事件。
例如 `document.createElement(...)`、`element.id`、`element.extra = 1`、`window.eval`、`'extra' in element`、
`Object.getOwnPropertyDescriptor(window, 'eval')` 都会产生事件。

暴露给 JS 的 Python 可调用对象（`Context.glob` 上挂的函数、绑定方法，例如 `chrome.loadTimes`）被调用时按 `method` 上报：
`receiver` 为该可调用对象所属的 Python 对象，普通函数为 `None`，`arguments` 与 `value` 的约定同 IDL 方法。

**值转换**：标量按值转换；原生 Python 对象保留身份；其他 JS 对象（包括数组、Proxy、异常对象）以 JSObject 系列引用传递。
不遍历对象或数组，不调用 getter、Proxy trap 或 `toString`。这些引用不是深拷贝快照；后续对象变化仍然可见。
默认 `format_trace` 对普通对象只显示类型（如 `<Navigator>`），对函数显示源码（如 `function eval() { [native code] }`）；
源码经绑定的 raw toString 取得，不会调用被观察对象自己的 `toString`。

未传回调时使用 `pyv8c.pyv8.trace.log_trace`，通过标准 logging 的 `pyv8c.pyv8.trace` logger 输出日志。
原生层缓存回调和事件名称，关闭时跳过观察作用域与值转换，不改变对象模板。
回调同步运行在当前 realm 线程，独立重入计数抑制回调内部再次产生 trace 事件。
回调返回值被忽略；回调自身的 Python/JS 异常被隔离，不替换业务结果或原异常；终止执行时不再派发。
需要诊断回调错误时，请在回调内部自行捕获并记录。

自定义回调仍是用户代码：显式读取 JSObject 属性、打印它、修改 receiver 或执行 JS 都可能产生副作用。
要保持被动观察，请只保存事件或使用安全的 `format_trace`，不要在回调内主动访问/修改 JS 对象。

## TypedArray 与 Promise

### TypedArray

JavaScript TypedArray 会转换为 `pyv8.JSTypeArray`：

```python
array = context.exec_js("new Uint8Array([10, 20, 30])")
assert isinstance(array, pyv8.JSTypeArray)
assert array.length == 3
assert array[1] == 20

array[1] = 99
```

从 Python 创建 TypedArray：

```python
array = pyv8.JSTypeArray(
    [1, 2, 3],
    pyv8.ArrayType.Uint8Array,
)
context.expose(array=array)
assert context.exec_js("array instanceof Uint8Array") is True
```

可用 `ArrayType`：

- `Uint32Array`
- `Uint16Array`
- `Uint8Array`
- `Int32Array`
- `Int16Array`
- `Int8Array`
- `Float64Array`
- `Float32Array`
- `Uint8ClampedArray`

Python `bytes` 会转换为 `ArrayBuffer`：

```python
context.expose(payload=b"\x01\x02\xff")
assert context.exec_js("payload.byteLength") == 3
```

### Promise

```python
promise = context.exec_js("Promise.resolve('success')")
assert isinstance(promise, pyv8.JSPromise)

promise.then(lambda value: print(value))
```

`JSPromise` 当前是 JavaScript Promise wrapper，不是 Python `asyncio.Future`，不能直接
使用 `await promise`。需要 asyncio 集成时，应在 Python callback 中把 resolve/reject
结果写入事件循环所属的 Future。

## 异常与超时

### JavaScript 异常

语法错误、运行时错误、getter/proxy 错误会转换为 `pyv8.JSException`：

```python
try:
    context.exec_js("throw new TypeError('bad input')")
except pyv8.JSException as error:
    print(error)
```

### Python 异常进入 JavaScript

错误层级属于绑定：`pyv8.JsError` 与它旁边的六个类型（`JsTypeError`、`JsRangeError`、
`JsReferenceError`、`JsSyntaxError`、`JsURIError`、`JsEvalError`）各以对应的 Python 内建为
基类。**让一个类继承其中一员，就是全部的声明**——没有表要安装，也没有名字要对齐：

```python
from pyv8c import pyv8


class PageTypeError(pyv8.JsTypeError):
    pass


def fail():
    raise PageTypeError("invalid payload")


context.expose(fail=fail)
result = context.exec_js(
    """
    try {
        fail();
    } catch (error) {
        [error.name, error.message, typeof error.stack];
    }
    """
)

assert result.to_python() == ["TypeError", "invalid payload", "string"]
```

- 页面看到的是**所继承的那一类**错误，由访问方 realm 自己的构造器建出来，所以
  `DOMException` 用的是访问方自己的那一个；
- 不继承这一族的异常（含调度与运行等宿主自身的失败）只有宿主看得见：桥接层会终止当前脚本，
  并在边界处把原异常抛回宿主；
- 一个恰好叫 `TypeError` 的宿主类不会因此被误映射——它没继承，就不会进 JavaScript；
- 形状不标准的类在自己的类上声明：`__v8_error_shape__`，一个三元组——realm 里的构造器名、
  跟在消息后的构造参数属性名（例如 `DOMException` 的 `("_name",)`）、建好后拷贝上去的
  `(Python 属性, JavaScript 属性)` 对。不声明即默认：按所继承的家族成员显示，无额外参数、
  无附加字段；
- 错误对象上只有浏览器语义的内容（名字、消息、声明过的字段）：宿主自身的调用链与文件路径
  不会被写到对象上，页面 `catch` 到它、或把它上报出去，都读不到宿主那一侧。

### 执行超时

```python
context.timeout = 0.1

try:
    context.exec_js("while (true) {}")
except pyv8.JavaScriptTerminated:
    pass

# 超时终止状态会被清理，同一 Context 可以继续使用。
context.timeout = 0
assert context.exec_js("6 * 7") == 42
```

也可以只限制一次调用：

```python
context.exec_js("runTask()", timeout=0.25)
```

超时通过 V8 TerminateExecution 实现，不是操作系统级内存或进程限制。堆预算采用安全点
上的协作终止（见 [堆上限与外部内存](#堆上限与外部内存)），不能保证避免所有进程级 OOM；分配失败、断言失败
和其它 fatal error 仍可能直接终止进程。

## 多 Isolate 与生命周期

### 创建独立 Isolate

```python
import gc
from pyv8c import pyv8


class Window:
    pass


isolate_a = pyv8.Isolate()
isolate_b = pyv8.Isolate()

context_a = isolate_a.context(Window)
context_b = pyv8.Context(Window, isolate=isolate_b)

context_a.exec_js("globalThis.name = 'A'")
context_b.exec_js("globalThis.name = 'B'")

assert context_a.exec_js("name") == "A"
assert context_b.exec_js("name") == "B"

del context_a, context_b
gc.collect()
isolate_a.close()
isolate_b.close()
```

每个 Isolate 有独立的：

- JavaScript heap
- Context 和全局对象
- FunctionTemplate 缓存
- V8 字符串缓存
- Python owner 集合

### 句柄不能跨 Isolate

以下对象不能从 Isolate A 传入 Isolate B：

- `JSObject`
- `JSFunction`
- `JSPromise`
- `JSTypeArray`
- `JSValue`
- `Script`

违反该约束会抛出 `RuntimeError: JavaScript value belongs to another isolate`。
跨 Isolate 传递数据应使用 JSON、普通 Python 容器或自行实现 structured clone。

### 线程

不同线程可以分别进入不同 Isolate，内部通过 V8 Locker 保护。Python callback 仍受 GIL
约束，因此多 Isolate 不等价于 Python 代码完全并行。

不要让同一个 Context 同时被多个线程无序操作。需要线程间调度时，由应用层明确管理
Context 所有权和消息队列。

### 关闭规则

`context.close()` 是幂等操作。关闭后，这个 Context 创建的 `JSObject`、
`JSFunction`、`JSValue`、Promise、TypedArray 和 `Script` 均失效；通过 Python
再次访问会抛出 `RuntimeError`。另一个 Context 保存的宿主函数、构造器、方法和
getter 也不能继续调用已关闭 Context 的 Python 回调，调用时会抛出 JavaScript 错误。
其他活跃 Context 的回调和错误钩子不受影响。

在 Python 回调内部关闭 Context 时，立即禁止新的访问，底层引用清理延迟至当前
原生执行退出。Context 和关联包装类型参与 Python 循环 GC，但应用仍应显式关闭
页面和 Context，以确定性释放线程、网络及原生资源。

`context.set_error_hooks()` 仅设置当前 Context 的钩子；Promise rejection 按
Promise 的创建 Context 投递。关闭 Context 自动解除自己的钩子。

`context.gc()`、`isolate.gc()` 和 `pyv8.v8_gc()` 使用生产可用的低内存回收通知，
不要求启用 `--expose-gc`，也不保证进程工作集立即下降。

关闭自定义 Isolate 前，必须释放它的所有持久句柄：

```python
context.close()
gc.collect()
isolate.close()
```

如果仍有活动 Context、Script 或 JS wrapper，`close()` 会抛出包含
`persistent handle` 的 `RuntimeError`，并保持 Isolate 可继续使用。
Context.close 会撤销其关联持久句柄，因此无需先删除已经失效的 Python 包装对象。
正在执行原生调用时不能销毁 Isolate，会抛出 `RuntimeError`。

`pyv8.shutdown_v8()` 用于关闭默认 Isolate 和进程级 V8 Browser：

```python
del context
gc.collect()
pyv8.shutdown_v8()
```

注意：

- 存在活动自定义 Isolate 时会拒绝 `shutdown_v8()`。
- 存在默认 Isolate 的活动句柄时会拒绝关闭。
- `shutdown_v8()` 不会为了关闭而启动 V8：从未使用过时直接返回 `False`。
- V8 Dispose 后不能在同一进程重新初始化，之后首次使用会抛出 `RuntimeError`。
- 第二次调用 `shutdown_v8()` 返回 `False`。
- subinterpreter 和 free-threaded CPython 当前被明确拒绝。

普通应用通常不需要主动调用 `shutdown_v8()`；只在需要确定性释放 V8 资源的独立进程
或测试中使用。

## DevTools 调试

### 启动调试服务

```python
from pyv8c import pyv8


class Window:
    __v8_idl_spec__ = pyv8.IdlSpec(
        global_this=True,
    )


def run_target(context):
    context.exec_js(
        """
        function decode(value) {
            const result = value.split('').reverse().join('');
            return result;
        }
        decode('payload');
        """,
        filename="reverse-target.js",
    )


context = pyv8.Context(Window())
try:
    with pyv8.start_devtools(context, options=pyv8.DevToolsOptions(port=9005)) as handle:
        print(handle.url)
        handle.run(run_target)
finally:
    context.close()
```

由调用者打印 `handle.url`，地址包含稳定的 Target ID，例如：

```text
devtools://devtools/bundled/inspector.html?experiments=true&ws=127.0.0.1:9005/devtools/page/<target-id>
```

将地址粘贴到 Chrome 地址栏。DevTools 发送
`Runtime.runIfWaitingForDebugger` 后，`handle.run(run_target)` 在调用线程执行回调。
默认首句暂停一次；Console 求值不会消耗这个暂停，重连也不会重复已消耗的暂停。

### 使用注意事项

- Chrome 130+ 的 DevTools CSP 只允许该页面连接 `ws://127.0.0.1:*`，默认地址不要改成
  `localhost`。
- 同一 Context 可以有多个独立的原生 Inspector session；服务保留单客户端连接模型。
- `start_devtools()` 不阻塞业务。独立 Context 使用 `handle.run()` 或 `wait_for_ready()` 在调用线程处理握手。
- 断线释放启动等待及暂停，保留 Context 和服务供重连；不再接管进程信号。
- `handle.close()` 在 Context 的调用线程执行，关闭服务但不关闭 Context；支持重复调用和上下文管理。
- `DevToolsOptions` 统一配置 host、port、wait_for_debugger、pause_on_start、startup_timeout。
- `filename` 决定 Sources 面板和堆栈中显示的脚本名称。
- DevTools 可能发送嵌入式 V8 不支持的 `Network.*`、`Target.*` 方法，返回 `-32601`
  不代表调试连接失败。

### 自定义协议桥接

需要完全控制 CDP 时，可以继承 `pyv8.Debugger`，实现：

- `handle(message)`：接收 Inspector 返回的 JSON 消息
- `on_ready()`：接收 `Runtime.runIfWaitingForDebugger`，不隐式安排暂停
- `run_loop()`：V8 暂停时运行嵌套消息循环
- `quit_loop()`：恢复执行时退出嵌套循环

普通使用者应优先使用 `pyv8.start_devtools()` 和 `DevToolsHandle`。
详细的生命周期、发现接口和迁移说明见 [DEVTOOLS](devtools.md)。

## API 速查

### 模块级 API

| API | 说明 |
| --- | --- |
| `pyv8.Context(...)` / `pyv8.V8Context(...)` | 创建 JavaScript Context（`V8Context` 是同一类型的别名，与宿主的 profile 区分开） |
| `pyv8.Isolate()` | 创建独立 V8 Isolate |
| `pyv8.Script(context, source, name=None)` | 编译可复用脚本 |
| `pyv8.new(constructor, *args)` | 调用 JavaScript 构造函数 |
| `pyv8.current_context(isolate=None)` | 获取当前执行 Context |
| `pyv8.incumbent_context(isolate=None)` | 获取 incumbent Context |
| `pyv8.v8_gc()` | 请求默认 Isolate 执行完整 GC |
| `pyv8.v8_version()` | 返回实际链接的 V8 版本；不启动 V8 |
| `pyv8.configure(params=None, icudtl_path=None)` | 指定下一次启动的 V8 参数与 ICU 数据；仅首次使用前有效 |
| `pyv8.is_initialized()` | V8 是否已经启动 |
| `pyv8.shutdown_v8()` | 永久关闭进程级 V8 Browser；不启动 V8 |
| `pyv8.start_devtools(...)` | 启动 Chrome DevTools bridge |
| `pyv8.Null` | JavaScript `null` 单例 |
| `pyv8.Undefined` | JavaScript `undefined` 单例 |

### Context

| API | 说明 |
| --- | --- |
| `exec_js(program, filename=None, timeout=None)` | 执行字符串或 Script |
| `expose(*objects, **named)` | 暴露 Python 值到 globalThis |
| `install(names)` | 把每个名字装成不可枚举的惰性构造器 |
| `define_global(name, value, attributes=0)` | 按指定属性位装一个全局值 |
| `hide_global(name)` | 撤掉一个已暴露的全局名 |
| `js_obj(py_object)` | 查找 Python 对象对应的 JS wrapper |
| `gc()` | 请求所属 Isolate 完整 GC |
| `glob` | globalThis 的 JSObject wrapper |
| `timeout` | Context 默认执行超时秒数 |

### Isolate

| API | 说明 |
| --- | --- |
| `context(global_this, ...)` | 在该 Isolate 创建 Context |
| `gc()` | 请求该 Isolate 完整 GC |
| `close()` | 无活动句柄时关闭 Isolate |
| `closed` | Isolate 是否已关闭 |
| `external_memory` | 已报给 V8 的隔离堆外内存字节数 |

## 测试

### 完整 Python 版本矩阵

```bash
python314 -m pip install --upgrade tox
python314 -m tox
```

tox 使用 Python 3.12 构建一次 ABI3 wheel，再分别安装到 Python 3.12、
3.13、3.14、3.15 环境。这样可以避免错误地让所有解释器加载源码目录中的同一个临时文件。
Windows 上已验证 Python 3.15 的 ABI3 wheel 和原生测试，其他平台仍需分别验证。

### 指定版本

```bash
python314 -m tox -e py312
python314 -m tox -e py314
python314 -m tox -e py315
```

### 原地快速测试

```bash
python setup.py build_ext --inplace --force
python -m pytest -q -n auto
```

（`pytest-xdist` 在 dev extras 里。本套件很小，并行与串行无实质差别——403 passed 实测 5.42 s vs 5.55 s——
按仓库统一习惯带上 `-n auto` 即可。）

指定测试：

```bash
python -m pytest tests/test_context.py -q
python -m pytest tests/test_chromium_contracts.py -q
python -m pytest tests/test_devtools.py -q
```

## 常见问题

### `DLL load failed: %1 不是有效的 Win32 应用程序`

通常表示 `_pyv8.pyd` 与当前 Python 的架构或 ABI 不匹配：

- 32 位 Python 加载了 64 位 `.pyd`
- Windows ARM64/x64 产物混用
- 文件实际不是 Windows PE 扩展
- 构建过程中复制了其他平台产物

检查：

```powershell
python -c "import struct, sys; print(sys.version); print(struct.calcsize('P') * 8)"
```

重新使用目标平台的 Python 和编译器构建 `_pyv8`。

### `ICU data does not match this _pyv8 build`

当前 `pyv8c/pyv8c/pyv8/icudtl.dat` 与 `_pyv8` 编译时使用的文件不同。不要只复制其中一个文件；
重新构建扩展，或者把同一 wheel 中的 `_pyv8` 和 `icudtl.dat` 一起恢复。

### `Fatal process out of memory` 或 native crash

这是 V8 的进程级 fatal error，不是 Python 异常，loguru、`try/except` 和 `sys.excepthook`
无法记录崩溃后的 Python 日志。

建议：

- 堆预算不能保证避免进程崩溃（见 [堆上限与外部内存](#堆上限与外部内存)）；真正的
  分配失败与 fatal error 仍是这里描述的情况
- 确保 ICU 数据和 V8 静态库匹配
- 给不可信脚本设置 `timeout`
- 将高风险任务放入独立子进程
- 使用 V8 内存参数限制进程资源
- 在进程外采集退出码、stderr 和 crash dump

### DevTools 显示 `WebSocket disconnected`

确认 URL 使用：

```text
ws=127.0.0.1:9005/devtools/page/<target-id>
```

直接使用 `handle.url`，不要丢弃 Target 路径。检查端口、防火墙以及独立 Context 是否正在
`handle.run()` / `handle.wait_for_ready()` 中处理命令。断线后可重新打开相同地址。

### 旧 DevTools 参数或 `DevtoolsDebugger` 无法导入

旧实现已移除。将 `host`、`port`、`startup_timeout` 放入 `DevToolsOptions`；
`callback=fn` 改为 `handle.run(fn)`。参见 [迁移表](devtools.md#旧接口迁移)。

### `cannot close V8 isolate while ... persistent handle(s) are active`

仍有 Context、Script 或 JavaScript wrapper 引用该 Isolate。删除这些对象并执行
`gc.collect()` 后重试，不要强制销毁仍在使用的 Isolate。

### `JavaScript value belongs to another isolate`

正在跨 Isolate 使用 JSObject、JSFunction、Script 等 V8 句柄。将数据转换为普通 Python
容器、JSON 或 structured clone 后再传递。

### Windows 编译时无法覆盖 `_pyv8.pyd`

某个 Python 进程已经加载该文件，Windows 会锁定正在使用的 DLL。退出对应 Python、测试
或 DevTools 进程后，再执行 `build_ext --inplace`。
