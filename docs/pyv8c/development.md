# 开发指南

**简体中文** | [English](../en/pyv8c/development.md)

[文档索引](../README.md)

## 环境与工作约定

`dev` extra、源码检查和 tox 使用 `pytest>=8.4` 与 `pytest-asyncio>=1.4,<2`。共享测试夹具通过该插件注册 Runtime 的事件循环工厂。

在仓库根目录使用 CPython 3.12 安装项目，并确认加载的是本仓库的代码：

```powershell
$env:PYTHONUTF8 = "1"
python312 -m pip install -e ".[dev,reference]"
python312 -c "import pyv8c; from pyv8c import pyv8; print(pyv8.__file__, pyv8c.__file__)"
```

构建前须配置 V8 构建目录，详见原生项目文档。使用 Python 3.15 时先按下方说明以 Python 3.12 构建 ABI3 wheel。

修改前记录仓库的工作区状态，保留已有修改。核对实际加载的原生二进制、解释器、IDL、环境档案和 Chrome；修改 C++ 后必须重建正在使用的扩展，不能只用旧二进制跑 Python 测试。

核心规则见[架构设计](architecture.md)：修复进入实际拥有行为和状态的层，Web API 不直接依赖 Page 控制器；缓存明确所属实例、失效和释放条件，不建立进程全局的页面可变状态。

## 源码构建

### Windows 本地构建

本机构建发行包时，先按 [pyv8 文档](../../pyv8/README.md)准备 V8，然后在仓库根目录使用 CPython 3.12：

```powershell
py -3.12 -m pip install build wheel
py -3.12 -m build --wheel --outdir dist .
```

生成的 `pyv8c-*-cp312-abi3-win_amd64.whl` 是单包发行物。

### Linux 本地构建

Ubuntu 22.04 x86-64 可以在 Linux 本机准备 V8 并构建 wheel，使用启用 GIL 的
CPython 3.12、C++20 编译器和 Python 构建工具：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel build
python setup.py build_v8
python -m build --wheel --no-isolation
python -m pip install dist/pyv8c-*-linux_x86_64.whl
```

如需代理，可向 `build_v8` 传入 `--proxy=http://主机:端口`。首次构建会下载
V8 和 Chromium 工具链并耗费较长时间及磁盘空间；可通过 `PYV8_V8_JOBS` 控制并行度。
上述 Linux wheel 已在 Ubuntu 22.04 x86-64 验证基本运行，不代表兼容所有 Linux 发行版。

### Windows、Linux 和 macOS CI

仓库的 GitHub Actions 工作流 `Build wheels` 可手动选择并并行构建三个平台的 wheel。
三个平台均使用 GitHub 托管 runner，Linux 使用 Ubuntu 22.04 x64，无需本地虚拟机。
在 **Actions → Build wheels → Run workflow** 勾选需要的平台并手动运行。对应产物为
`windows-wheels`、`linux-wheels` 和 `macos-arm64-wheels`。

三个平台的 V8 缓存 key 由 SDK/GN 参数/V8 源码哈希加 runner 镜像版本组成：镜像版本放在
末尾，因此镜像更新后仍会按前缀回退到同一 SDK、GN 参数和源码的上一次缓存，避免重编 V8；
只有这些构建输入本身变化时才需要下载并重新构建 V8，耗时较长且需要足够磁盘空间。
回退还会按旧 key 布局做一次精确匹配，以兼容调整前已保存的缓存（迁移期只需重编一次）。
恢复的缓存会先校验 GN stamp。可用资源及缓存效果需在 Actions 中确认。
各平台任务安装 wheel 后运行冒烟测试、音频与 DOM 原生组件回归，以及原生崩溃冒烟；Linux 和 macOS 另检查原生参数桥接，Windows wheel 测试矩阵另跑架构层门禁。这些都不代表浏览器完整测试全部通过。

## 实现接口

接口声明使用 `pyv8c.idl.interface`。方法常用 `fn_name`，属性常用 `get_name`、`set_name`；named/indexed 与跨源 handler 使用对应 `__v8_*__` 约定。成员表面与属性位以 IDL 为依据，Python 负责参数转换、状态及实际语义，不重复手写基线已经确定的形状。

Feature 的通用运行机制位于 `pyv8c/pyv8c/features/runtime.py`，内置声明位于 `pyv8c/pyv8c/features/catalog.py`，核心接口组装位于 `pyv8c/pyv8c/features/core.py`。Feature 实现模块显式导出 FEATURE，并在 FeatureSpec 中声明依赖。新增可选功能不应使基础包导入时加载全部实现或原生运行时。纯元数据、提取器及基础资源记录仍需支持无原生环境导入。Web API 的目录归属见[架构设计](architecture.md#web-api-归属)。

开发前先建立最小失败场景，再修复和添加回归测试。普通 JS 错误、宿主故障、执行终止与资源失败分别处理；宽泛捕获 BaseException 时必须确认是否需要传播执行控制信号，不能靠吞掉终止后继续页面流程保证宿主存活。

异步回调校验取消、文档代次和身份。真实网络、Python 阻塞及工作线程结果回投不能绕过 owner 规则。新增公共接口同时更新类型声明和调用方；新增生命周期记录必须有统一终态和释放出口。

## 测试通道

```powershell
# 从仓库根目录运行原生模块与框架测试，不含 slow
python -m pytest -q

# 只运行浏览器框架测试
python -m pytest -c pytest.ini pyv8c/tests -q

# 不运行 Chrome 对照的常规主套件（含原生崩溃冒烟）
python -m pytest -c pytest.ini pyv8c/tests/unit pyv8c/tests/integration pyv8c/tests/browser pyv8c/tests/native -q

# 只跑原生崩溃冒烟（独立子进程）
python -m pytest -c pytest.ini pyv8c/tests/native -q

# 慢速生命周期、堆预算及策略复查
python -m pytest -c pytest.ini pyv8c/tests -o addopts=--strict-markers -m slow -q

# 标记竞态用例至少重复五轮
python -m pytest -c pytest.ini pyv8c/tests -o addopts=--strict-markers -m race --count=5 -q

# 架构、改动配套、启动、离线导入和重复用例检查
python tools/check_policy.py
```

默认 pytest 配置排除 slow，所以 `-m slow` 同时覆盖 addopts，避免选择器冲突。竞态测试也可能标记 slow，重复运行时同样需要覆盖。不要默认并行运行 Chrome 对照。

修改原生支持时，在仓库根目录运行构建、测试和布局检查：

```powershell
python312 setup.py build_ext --inplace
python -m pytest -c pytest.ini pyv8/tests -q
python pyv8/tools/check_layout.py
```

构建需要该仓库要求的 V8 SDK、编译器和相关设置。不要在其他进程仍加载目标二进制时覆盖它，也不要清理非本次创建的进程。

## 测试分层与归位规则

`pyv8c/tests/` 下五个目录不是按功能模块划分，而是按「跑起来需要什么」和「断言的判据由谁给」分层。这套划分被 `tox.ini`、`pytest.ini` 和 `tools/check_policy.py` 直接引用，新增测试放错层会破坏门禁。

| 目录 | 判据来源 | 运行环境 | 典型内容 |
| --- | --- | --- | --- |
| `unit` | 纯 Python 断言 | 必须能在没有已编译扩展时导入 | 架构静态检查、IDL/元数据、参数转换、CookieJar、工具脚本 |
| `browser` | 单个 Web API 在 pyv8c 里的返回值 | 离线（`default_loader` 返回空 200），不用 Playwright | DOM/BOM 接口、事件、存储 |
| `integration` | 多个子系统接起来是否还成立 | 本地 HTTP/WebSocket server、Worker、定时器、线程拆除 | 导航、资源、执行预算 |
| `native` | 子进程是否活着 | 独立进程 + `-X faulthandler` | `test_native_smoke.py`；C++ 段错误会直接杀死 pytest 进程，只能在进程外断言退出码 |
| `repros` | 真实 Chrome 与 pyv8c 是否一致 | 本机 Chrome + Playwright（`reference` extra） | `corpus.py` 的行为语料，同一 case 两侧各跑一次 |

归位规则：不碰 V8 → `unit`；单个页面行为 → `browser`；跨组件或真实 I/O → `integration`；担心 C++ 崩溃 → `native`；要跟 Chrome 对答案 → `repros`。

三条硬约束：

- `tox.ini` 的 py312~py315 wheel 矩阵只跑 `pyv8/tests` + `tests/unit` + `tests/browser` + `tests/integration`，刻意排除 `native` 和 `repros`：矩阵验证的是同一份 abi3 wheel 在四个 CPython 版本上的行为，不能依赖本机 Chrome，也不必把子进程冒烟重复四次。
- `tools/check_policy.py` 的 `offline` 门设置 `PYV8C_OFFLINE=1`，由 `tests/conftest.py` 屏蔽 `pyv8c.pyv8` 后只跑 `tests/unit`。因此 **unit 中任何顶层导入会连带加载原生扩展的测试文件，都必须在所有 `pyv8c` 导入之前写一行 `pytest.importorskip("pyv8c.pyv8")`**。该门先做静态预检：按包内 import 图的传递闭包算出「原生绑定模块」（含 `pyv8c._native` 及其兼容入口），报出违规的文件与行号。`@pytest.mark.native` 只表示「此用例需要已编译扩展」，不能替代这行守卫，因为收集阶段就已经失败了。干净的 CI checkout 同样要设 `PYV8C_OFFLINE=1`，否则 `pytest.importorskip` 会把 `cannot import name '_pyv8'` 重新抛出，而不是跳过。
- `pytest.ini` 注册 `native` / `race` / `slow` 三个 marker 并默认 `-m "not slow"`。**`@pytest.mark.native` 与 `native/` 目录不是一回事**：前者是「需要扩展」的标记，`unit` 里也有；后者表示必须在独立子进程里验证不崩溃。

`repros` 同时验证 pyv8c 的固定预期和双引擎行为。默认 `pytest -q` 也会收集它。常规对照使用已验证的系统 Chrome **158.0.8127.36**；权限策略清单、性能条目、跨文档动画帧和请求头对照使用 Chrome for Testing **154.0.8030.0**，与 IDL 基线一致。独立采集的 `chrome-environment.json` 仍使用 **156.0.8078.80**。排查版本差异时，用同一用例分别运行基线和新版，保留原有 `expected`。

`native/` 已接进 `Build wheels` 工作流：Windows wheel 测试矩阵、Linux 与 macOS 构建任务在安装 wheel 后各跑一次原生崩溃冒烟，Windows 矩阵另在 Python 3.12 上跑 `layers` 架构门；源码树里没有编译扩展时，冒烟测试自动从已安装的 wheel 加载，本地有扩展时仍按原来的源码路径运行。`layers` 门会启动子进程真实导入 Web 模块，`startup` 门要构造 Browser，二者都需要编译扩展，因此留在 wheel 任务与本地 pre-commit；`.github/workflows/source-checks.yml` 的 PR 门禁只跑 `check_license.py`、三个纯源码策略门和离线 `tests/unit`。

## 发布 wheel

在 GitHub Actions 的 `main` 分支运行 **Build wheels**，启用 Windows、Linux 和 macOS。Linux x64 在 Ubuntu 22.04 构建，由 `auditwheel` 修复到 `manylinux_2_35_x86_64`，随后安装并测试修复后的 wheel。

三平台构建和测试都通过后，在同一 commit 上运行 **Publish pyv8c wheels**。将构建运行 URL 中的数字填入 `build_run_id`，先选择默认的 `testpypi`。工作流会核对构建来源、commit、包名、版本和平台标签，再上传。演练安装时，从 TestPyPI 下载 pyv8c wheel，然后通过常规 PyPI 安装其依赖。

在 TestPyPI 和 PyPI 分别配置 GitHub Trusted Publisher。首次发布用 Pending Publisher。项目名填 `pyv8c`，Owner 填 `ConlinH`，Repository 填 `pyv8c`，Workflow 填 `publish.yml`。Environment 在 TestPyPI 填 `testpypi`，在正式 PyPI 填 `pypi`。在 GitHub 中创建同名环境，并将部署来源限制为 `main`。

TestPyPI 安装验证通过后，用同一个 `build_run_id` 手动选择 `pypi`，并在 `confirm_version` 输入完整标签（如 `v0.2.1`）。正式 PyPI 上传成功后，工作流给构建 commit 打标签，生成 GitHub Release 说明，附上同一批 wheel 并核对 SHA-256。TestPyPI 演练不创建标签或 GitHub Release。如果 PyPI 已成功而 GitHub Release 失败，在原运行中选择 **Re-run failed jobs**，即可从已有标签和草稿继续。已有标签指向其他 commit 或附件哈希不符时，工作流会停止。

## Chrome 行为对照

```powershell
$env:PYV8C_CHROME = "C:\Program Files\Google\Chrome\Application\chrome.exe"
$env:PYV8C_CHROME_BASELINE = "C:\path\to\chrome-154.0.8030.0\chrome-win64\chrome.exe"
python -m pytest -o addopts=--strict-markers -q -ra
```

未设置 `PYV8C_CHROME_BASELINE` 时，相关用例也使用 `PYV8C_CHROME`；两者都未设置时查找系统 Chrome。缺失 Chrome 或 Playwright 的用例可能跳过，跳过不能算通过。上述命令包含 `slow` 用例。语料对照使用软件渲染读取清屏像素，WebGL 扩展对照验证清单唯一、可获取及 WebGL 版本语义；本地固定预期仍检查扩展数量。设备枚举、音频采样率及客户端提示使用双方可复现的输入。

两侧使用同一 HTML、origin、响应头、脚本和操作序列。真实访问用于发现问题，本地服务器或 fixture 用于稳定验收；fixture 只服务明确资源，未知请求失败。先比较导航、资源、脚本、事件和就绪条件，定位最早因果分歧，再检查接口表面或环境分支。

区分框架缺陷、功能缺失、配置/版本/设备差异、服务端响应及探针干扰。WebGL、媒体、权限和网络差异尤其需要记录环境因素。页面的环境检查应提炼为可重复的浏览器语义，不用常量成功、站点补丁或替换脚本代替框架修复。

## 提取与性能

IDL 和环境采集命令见 [IDL 格式](idl-format.md)。生成产物前核对来源版本，先写独立输出验证；不要无授权覆盖 Chromium/V8/profile 基线。

Window 顺序来自环境档案的 `bindings.own_order`；更新顺序应重新采集目标 Chrome，记录版本和采集条件。IDL 只记录接口与成员，不再保存单独的 Window 顺序基线。运行时统一安装实际暴露的属性，不能满足的 V8 内置属性顺序由差异诊断报告。

性能先剖析再优化，同机同解释器、同源码/二进制标识和同工作负载串行交替测量至少五轮，记录中位数、最小值、波动和关闭后回收。无稳定收益或引入行为回退的改动不保留。

```powershell
python benchmarks/benchmark.py --runs 5 --output .\benchmark-result.json
python benchmarks/benchmark.py --cases startup_offline,cold_compile,page_cycle --runs 5 --output .\benchmark-startup.json
python benchmarks/benchmark.py --list
```

这些命令会运行测量。`benchmark.py --record` 和策略工具的 `--record-startup` 会改写性能基线，不能用它们消除回退。环境变化和工作负载变更需要单独说明，不能直接比较不等价数字。

## 实现现状

本节描述 Python Web API 层当前的组织方式与边界，便于把改动放到拥有该状态或行为的层。原生桥接与线程归属见[桥接内核](../pyv8/bridge-kernel.md)，任务与唤醒协议见[任务调度](task-scheduling.md)。

### Web API 与选择器

普通 IDL 方法直接绑定回调，不额外引入参数包装层。`DOMString` 对原生 Python 字符串直接返回，JS 对象仍执行实际转换；可选属性钩子与观察者状态显式初始化为 `None`，未观察属性的写入不会反复产生 `AttributeError`。

查询与样式层共用 `matches_group`，选择器匹配不为每个候选创建自引用递归闭包。完整查询按最右标签缩小候选集，分组查询保持树顺序与去重，首项查询惰性遍历并立即返回。实现复用已有 AST 和树版本索引，每次读取当前属性和父子关系，不存放跨查询的 DOM 结果缓存。

### HTML 解析与 DOM 构造

扫描器在原始输入的偏移处做正则匹配，复用标准库属性分词器，每个开始标签只生成一份解码记录；消费后的记录立即退休，完成后清空 token 缓冲。脚本阻塞时保留未消费记录，`document.write` 继续在插入点处理。普通节点的插入、移除、shadow root 和 parser-created 内部状态采用明确默认值，子类与实例钩子仍动态查找；采用、资源发现、事件和失效通知不被跳过。

DOM C++ 代码位于 `pyv8c/native/dom`，与 `pyv8c/native/src/audio` 的 Rust 音频代码一起链接到 `pyv8c._native`。扩展使用 CPython 3.12 stable ABI，无 V8 链接依赖。项目内部使用 `from pyv8c._native import audio, dom`。DOM 组件初始化 EventTarget/Node 基础字段及 live child view，并在活动文档的普通 body 构造中按批分派与插入普通 HTML token（每批至多 1024 个）。节点构造器仍是原有 Python 类，每次插入仍执行树缓存失效；自定义 setter、描述符及初始化期间更换 setter 的行为保留，每次插入还核对父节点当前绑定的插入与采用方法。只有全新、同文档、无插入钩子且无 shadow tree 的普通节点走直接插入，其余进入正常树操作。脚本、事件属性、资源元素、模板、raw text 和外来命名空间继续由语义树构造步骤处理；构造器执行前退休当前 token 并更新 parser 位置，每批检查操作控制，保留超时、停止和信号处理。

Python tokenizer 仍然保留，当前树构造算法不是完整 HTML5 解析器；DOMParser 与片段解析沿用同一套语义步骤。

### 启动与 IDL 装配

IDL 成员查找为每个文档视图按需建立子接口成员索引，不对每个缺失名称重复遍历子接口。祖先优先、子接口文档顺序、main-world 优先及按位置应用的档案省略规则保持不变；基类声明按所属接口复用，无接口声明的 mixin 仍按目标接口区分。声明缓存按文档视图隔离并采用弱键，随视图释放，避免同一文档的不同环境档案相互混用。导入时机保持不变。

### 样式计算与布局

文档与 ShadowRoot 的规则缓存保存 `StyleRules`：按每个选择器最右复合项中必要的 ID、类或标签筛选候选，再按原始规则顺序执行完整匹配。分组选择器保持整条规则及最高匹配 specificity，通用项仍参与匹配；全部规则均无可索引项时按已有顺序遍历。索引不保存元素或匹配结果，读取时使用当前属性与祖先关系，沿用各树的 `style_version` 失效机制。

实际应用的样式源已统一：插入/删除规则及规则声明变更均通知所属文档，DOM 文本保持其独立语义。`CSSFontFaceRule` 保留字体声明，编辑脱离文档的旧样式表不会影响替换后的样式表。现有选择器、条件规则与布局算法的支持范围不变，不等同于完整 Chromium 渲染实现。

### 同线程 Browser 与双入口

公开入口分为 `pyv8c.sync_api` 和 `pyv8c.async_api`，共用同一套同线程 Browser/Profile/Page
实现；顶层根包不再导出 Browser，两套入口的对象不可互换。Page 与 iframe 固定在应用 owner，
Worker 使用独立线程。导航、宿主 HTTP 和等待在异步入口通过 await 协作推进，在同步入口
直接返回结果；页面操作与活句柄仍不允许跨线程调用。Browser 关闭停止任务接收并汇合 Worker、
网络和加载器线程；关闭等待者取消不会取消资源清理。两套入口共用执行内核，不做旧线程模型的
自动切换或回退。

Windows / CPython 3.12.9 / V8 15.4.68 五轮串行交替测量，原生扩展摘要一致。
每项预热 200 次、测量 12000 次；以下是每轮总墙钟时间除以操作数的五轮中位数，单位 μs/次。

| 负载 | 重构前线程模型 | Browser | 耗时变化 |
| --- | ---: | ---: | ---: |
| evaluate | 64.421 | 8.489 | -86.8% |
| result | 87.086 | 21.949 | -74.8% |
| ua | 74.599 | 13.688 | -81.7% |
| dom | 84.916 | 21.822 | -74.3% |
| cooperative | 69.331 | 12.134 | -82.5% |
| concurrent | 68.003 | 10.866 | -84.0% |
| timer_load | 77.551 | 12.843 | -83.4% |
| native | 1.637 | 1.585 | -3.2% |
| script | 4.298 | 3.737 | -13.0% |

原始记录位于 `build/runtime-cutover-measurements-final-20261117`。这些结果只代表本机，
不能代表 Linux、macOS 或与其他 V8 版本的比较。Linux/macOS 按本阶段要求未验证。
正确性和发布前检查使用完整测试集，并单独列出重构前已存在的失败。

## 交付要求

说明具体问题、触发条件、所属层修复、回归测试与实际验证结果。区分已通过、未验证、跳过和已知失败；保存竞态失败原始证据，不靠复跑成功宣称根因消失。不放宽断言、扩大排除或更新基线掩盖失败。

文档只维护当前可用接口与已知限制。命令、链接和示例变更后需验证；专题文件统一使用小写短横线命名，索引使用 README.md，正文统一中文。
