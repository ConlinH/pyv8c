# hCaptcha 本地执行案例

验证日期：2026-11-13，Asia/Shanghai。目标页面为 `https://dashboard.hcaptcha.com/login`，入口为 [hcaptcha.py](hcaptcha.py)。案例目录只分发本 README 和 Python 入口，分析过程、框架改动与验收结果统一记录在本文中。

入口通过 pyv8c 的本地 V8、DOM、iframe 和 Web API 执行当次下载的原始 SDK，由 SDK 获取配置、运行 HSW 及其中的 WASM，再提交并处理服务端校验。运行仍需联网；没有把 HSW 内部算法重写为纯 Python，也不启动浏览器或读取 HAR、保存的 SDK、浏览器 Cookie、指纹和历史 Token。

初始交付版的五个独立新会话均取得 `P1_` Token。当前 INFO/SUCCESS 版本提交前复测为 1/2 通过：一次 `network-error`，一次返回长度 2476 的 `P1_`。这些结果不能解释为持续通过保证，完整数据见下文。

## 运行方式

在工作区 `G:\pyv8env` 执行：

```powershell
.\.venv\Scripts\python.exe -B pyv8c\examples\hcaptcha\hcaptcha.py
```

也可以从案例目录导入：

```python
from hcaptcha import run

token = run(timeout=60, proxy="http://127.0.0.1:6696")
assert token.startswith("P1_")
```

默认代理为 `http://127.0.0.1:6696`；`proxy=None` 表示直连。下文最终验收均使用该代理，不应将结果直接套用于直连。`timeout` 是页面结果等待时限，HTTP 单次请求超时为 20 秒，并非整个函数的硬退出时限。

依赖为当前仓库的 pyv8c 及匹配的 `_pyv8` 和 `_native` 原生扩展。安装 pyv8c 时会安装 AES-GCM 所需的 PyCryptodome。迁移环境时参考[原生组件构建说明](../../pyv8c/native/README.md)。

成功时标准输出只打印完整 Token，退出码为 0；失败时退出码为 1。过程日志写到标准错误，格式为 `HH:mm:ss | LEVEL | message`：

- `INFO`：sitekey、关键 JS 和 iframe 地址、HTTP 状态与字节数、配置版本 v、pass、挑战类型、当次完整 req，以及 getcaptcha 请求响应大小。
- `SUCCESS`：取得的 Token 前缀和长度。
- `ERROR`：CLI 失败原因。

程序本身不保存源码、req、Token 或请求记录，不添加应用层自动重放校验 POST，也不在失败后调用浏览器回退。

## 请求链与参数来源

```text
目标 origin 下的本地最小页面
  → 当前 /1/api.js
  → SDK 创建 hcaptcha.html iframe
  → checksiteconfig 返回当次 c.req 等配置
  → SDK 按配置选择并加载当前 hsw.js
  → 本地执行 HSW 与其中的 WASM
  → SDK 生成二进制请求体并 POST getcaptcha
  → SDK 处理及解密响应
  → hcaptcha.execute 的 Promise 返回 Token
  → Python 检查 P1_ 前缀并返回
```

`SITEKEY` 来自应用公开配置中的 `userAccessSitekey`，属于案例的静态配置。c.req、SDK 版本路径、HSW 地址、运算输入和最终 Token 均由当次配置及原始 SDK 取得或生成，没有固定历史挑战参数或成功响应。

HAR 中 getcaptcha 的请求体和响应均为二进制内容。交付让原始 SDK 完成整条处理链，没有拼接历史请求，也不以 HTTP 200 或任意非空字符串作为通过条件。

## 请求范围

资源拦截器只允许以下 HTTP 或 HTTPS 请求，其余返回框架的本地空响应；`blob:`、`data:` 等非 HTTP 资源交给框架处理。

| 主机 | 允许路径 | 用途 |
| --- | --- | --- |
| `js.hcaptcha.com` | `/1/api.js` | 当前 SDK 入口 |
| `newassets.hcaptcha.com` | 以 `/hcaptcha.html` 或 `/hsw.js` 结尾 | iframe 与 HSW |
| `api.hcaptcha.com` | `/checksiteconfig`、`/getcaptcha/…` | 配置、校验及相应预检 |
| `*.w.hcaptcha.com` | `/logo.png` | SDK 网络探测 |

最小页面不加载控制台应用、登录接口、展示图片、字体或业务统计。遇到额外挑战时不扩展为点选求解。

## 分析过程

### HAR 与浏览器对照

分析使用 `hello_js_reverse_skill` 3.9.0、Camoufox MCP 1.9.4 和 Firefox 152.0.4-beta.29；交付使用 Python 3.13.16、pyv8c 0.1.0 及内置 V8 扩展。浏览器及 HAR 只提供分析依据，不提供运行输入，分析浏览器已关闭。

先从 HAR 确认公开 sitekey 及 `api.js → hcaptcha.html → checksiteconfig → hsw.js → getcaptcha` 链。HAR 的应用请求中存在 `P1_` Token，但没有复用该值。

浏览器加载真实页面后，显式调用已有的 `hcaptcha.execute`，得到长度 2478、前缀为 `P1_` 的对照结果，并观察到 getcaptcha POST 返回 HTTP 200。这次结果是在显式调用后取得的，不能写成首次打开页面就自动完成。网络采集在首次导航后启动，捕获 25 个请求，结束时未报告丢弃或待完成请求；它不是页面全部请求的完整清单。

### HSW 与 WASM 核查

保存的浏览器 hsw.js 为 1,638,472 字节，路径目录为 `a4d8bab5a6d640ffc420104fd1bd82e3f1ace50377f1a8efe86ad622baaca3b9`，SHA-256 为：

```text
43f69d81709c0b725015524b1eeb9b28a49ec8d27b6e07dba373ec11e30be61d
```

早期直连探针还取得过目录 `f55bf4a59e9accfad8ed13bb00440390c0d38107cf866f64661caa3d3f3eb974` 下的 1,651,789 字节脚本。应跟随当次配置选取资源，不能仅按固定更新时间、保存的文件名，或这两个版本的差异判断服务端拒绝原因。

源码中存在 `window.hsw` 入口和数字模式分支，交付仍通过公开 SDK 入口执行，由 SDK 提供完整输入并处理结果。

最初按 `AGFzbQ` 特征截取 Base64，虽然得到了 WASM 魔数，`WebAssembly.Module` 却报告代码段越界；该片段不能视为完整可执行模块。随后在独立离线分析页面执行当时本地保存的 HSW，在 `WebAssembly.instantiate` 调用边界取得实际交付给引擎的字节：

| 项目 | 结果 |
| --- | --- |
| 模块字节数 | 1,100,710 |
| SHA-256 | `d045225493c119e3520b1e61e4941f0b9a4a491e29917370c5ff7680e64ada95` |
| imports | 196，模块名为 `a` |
| exports | 23，包含函数和 memory |
| 独立检查 | Node 的 `WebAssembly.Module` 接受该模块 |

这次提取使用的是早期本地分支脚本，不能与最终浏览器快照混同为同一版本。模块构造及接口枚举成功不等于已还原全部内部算法；保存的源码和 WASM 不参与交付运行。

### 探针与缺口定位

| 阶段 | 实际现象 | 处理及结论 |
| --- | --- | --- |
| 初始探针 | Python 序列化 `pyv8.Null` 报错 | 改用 JS `JSON.stringify`；这是诊断程序问题 |
| 第二次探针 | 当时的 AES-GCM encrypt 返回 `NotSupportedError` | 定位到框架缺少 AES-GCM；现由 pyv8c 实现 |
| 第三次探针 | 创建音频压缩节点时缺少原生音频组件 | 构建现有音频扩展 |
| 第四次探针 | api.js 连接超时、加载失败 | 单独记录网络失败，分析探针对 GET 做有限重试 |
| 第五次探针 | FontFace.load 错误接收者触发宿主 `AttributeError` | 与浏览器最小用例对照后修正 Promise 拒绝行为 |
| 第六次探针 | 直连存在 GET 重试，结束时没有 Token | 不作为通过样本，也不据此判定算法一定错误 |
| 第七次探针 | 6696 代理下 getcaptcha HTTP 200，返回长度 2448 的 `P1_` | 首次本地通过，随后收敛入口并独立复测 |

分析探针的源码保存、详细诊断及额外挑战分支没有进入最终交付，探针的 GET 重试逻辑也没有保留。修复消除了各自可复现的异常，但没有足够的服务端对照证明某一修复单独保证通过。

## 适配与框架修复

### AES-GCM

案例直接使用 pyv8c 的 `SubtleCrypto.encrypt` 和 `decrypt`。框架通过 PyCryptodome 执行 AES-GCM，接收 IV 以及可选的 `additionalData` 和 `tagLength`，认证失败时以 `OperationError` 拒绝 Promise。

排查中曾看到 `_options(algorithm).keys()` 为空。该对象是惰性 Dictionary，成员通过 `.get()` 等操作从 JS 读取；Python 键集合为空并不意味着 SDK 没有传入 IV，不能据此添加固定参数。

已用 128 位全零密钥、12 字节全零 IV 和 16 字节全零明文检查已知答案，期望密文及认证标签为：

```text
0388dace60b6a392f328c2b971b2fe78
ab6e47d42cec13bdf53a67b21257bddf
```

框架的[浏览器测试](../../pyv8c/tests/browser/test_subtle_aes_gcm.py)覆盖已知答案、`ArrayBuffer` 返回类型、带偏移的视图和篡改后的拒绝。接口语义参考 [Web Crypto AES-GCM 操作](https://www.w3.org/TR/webcrypto/#aes-gcm-operations)。

### 原生组件构建

动态压缩节点由 `_native` 的 Rust 音频组件提供。使用 CPython 3.12、Rust 和 C++17 编译器在仓库根目录构建：

```powershell
python312 setup.py build_rust --release --inplace
```

构建在 `pyv8c._native` 包内生成 `_native.pyd`。项目代码通过 `from pyv8c._native import audio` 导入音频组件。

### FontFace 接收者修复

浏览器中，`FontFace.prototype.load.call(window)`、普通对象及 `null` 均返回 Promise，随后以 `TypeError` 拒绝。原框架在 Python 的 `fn_load` 中读取接收者的 `_status`，导致宿主 `AttributeError`。

在 [fonts.py](../../pyv8c/pyv8c/web/dom/fonts.py) 增加非法接收者检查，使用当前 Context 的 PromiseResolver 返回以页面 TypeError 拒绝的 Promise，保留正常加载路径。[字体回归](../../pyv8c/tests/browser/test_font_faces.py)新增三类接收者断言，字体模块共 13 项通过。这是本次实际修改的框架行为，源码与测试按框架目录正常提交。

## 案例结果与验收

以下记录均为 2026-11-13 的实测。每次独立调用新建 Browser、Profile 和 Page，最后关闭 Browser；没有读取分析目录或复用浏览器认证数据。

### 初始交付版

增加日志前的入口 SHA-256 为 `dfbfafa70d0234eba8f87953bfa56603b5693ad5321a37e49912f8e3d089c698`。连续五次调用均通过：

| 次数 | 结果 | Token 字符数 | 耗时秒 |
| --- | --- | ---: | ---: |
| 1 | `P1_` | 2434 | 19.35 |
| 2 | `P1_` | 2427 | 7.49 |
| 3 | `P1_` | 2450 | 6.29 |
| 4 | `P1_` | 2475 | 5.60 |
| 5 | `P1_` | 2484 | 5.60 |

另一次直接 CLI 调用退出码 0，Token 长度 2482，耗时 7.79 秒，标准错误为空。这次 CLI 检查独立于上表。

### 日志版本

首个日志版的源码 SHA-256 为 `6b03502d6ae83212aeafd30b760ac50c5a1b148d4eb83aa2298ee627c0245414`。该版独立 CLI 退出码 0，返回长度 2475 的 `P1_`，耗时 6.71 秒，9 条过程日志包含预期字段。

之后统一为 Loguru 的 INFO、SUCCESS 和 ERROR 样式，当前入口 SHA-256 为 `340eedc003a6cd9e25caf9920a0f383b8f09bb39d62a7e2685ae9f313548230e`。离线检查确认日志格式和传输层返回的响应对象身份；该检查使用本地替身数据，不计入线上样本。提交前对当前版做了两次独立 CLI 复测：

| 次数 | 结果 | 退出码 | Token 字符数 | 耗时秒 |
| --- | --- | ---: | ---: | ---: |
| 1 | `network-error` | 1 | 0 | 9.55 |
| 2 | `P1_` | 0 | 2476 | 10.36 |

第一次未出现成功的 getcaptcha 日志；第二次观察到 getcaptcha HTTP 200、8 条 INFO 和 1 条 SUCCESS。当前复测为 1/2 通过，失败结果保留在本文，不能被历史五次成功覆盖。

### 框架回归与策略检查

案例提交前重跑音频及字体回归，共 43 passed；策略六道门 6/6 通过，其中无原生扩展 unit 为 378 passed、41 skipped、2 deselected，启动中位数 399.3 ms，低于 834.5 ms 预算。

同步远程 main 的文本整形、构建及离线检查更新后，音频、字体、文本整形、架构和延迟模块共 116 项回归通过；策略六道门仍为 6/6，通过的无原生扩展 unit 为 367 passed、53 skipped、2 deselected，启动中位数 428.9 ms，预算 834.5 ms。两组统计对应不同的仓库版本。

在仓库根目录可重跑本案例直接关联的框架回归：

```powershell
..\.venv\Scripts\python.exe -B -m pytest pyv8c/tests/unit/test_audio_native.py pyv8c/tests/browser/test_font_faces.py -q -p no:cacheprovider
```

本次将报告合并为单个 README，只调整文档和仓库中的案例附件，不修改入口或框架行为，也没有重新发起线上校验。

## 已知限制与分析记录

`network-error` 是 SDK 的通用失败码，不能单独证明断网或 HSW 算错；请求失败、服务端错误和额外挑战后续资源受限都需要结合当次日志区分。本次失败样本不足以确定其具体根因。

通过条件为当次原始 SDK 返回非空 `P1_` Token，HTTP 200 本身不足以判定成功。所得 Token 没有进一步用于账号登录或其他业务接口；结果不代表所有网络环境或后续 SDK 版本都能通过。sitekey、资源路径或 SDK 契约变化时，应重新核对配置及白名单。

原始 SDK 快照、网络摘要、WASM、诊断日志和脱敏 JSON 验收摘要保存在本机工作区 `artifacts/hcaptcha-20261113`，未随本案例分发，运行入口不会读取它们。原先独立的过程文档与验收 JSON 已从案例跟踪文件中移除，关键信息合并到本文；框架修复和回归测试继续保留在原目录。
