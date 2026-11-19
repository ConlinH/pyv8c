# SteamDB 登录页案例

已于 2026-11-18 在本机通过真实线上验证。交付入口为本目录的 `5s.py`，依赖本项目 pyv8c，不依赖 Chrome、Camoufox、Playwright、HAR 或 cfsolve。

## 运行

在 `G:\pyv8env\pyv8c` 下执行：

```powershell
G:\pyv8env\.venv\Scripts\python.exe -X utf8 -B examples/5s/5s.py --proxy http://127.0.0.1:6696 --timeout 120
```

每次创建新 profile，由框架下载并执行线上原始脚本，通过框架鼠标接口点击验证，再由原始 SDK 继续请求和导航。成功返回退出码 0，失败返回 1。日志不打印 Cookie 或挑战参数，不保存浏览器产物。

`run(proxy=..., timeout=...)` 返回值的 `html` 字段为本次线上响应的真实正文，保存在内存中；其他字段包括 `status`、`method`、`url`、`html_bytes`、`html_sha256`、`document` 和 `elapsed`。

成功时 CLI 输出请求方法和最终 URL、HTTP 状态，并以“页面标题：…；正文 … bytes；Content-Type=…”一行结束。后续正文检查明细、登录控件、正文段落、哈希及统计不打印；这些数据仍可从 `run()` 返回值读取。Cookie 和挑战参数保持不打印。

新增输出后，2026-11-18 20:51:27 再次以新 profile 线上通过：POST 200、35,681 字节、六项检查通过，点击 1 次，耗时 19.36 秒，退出码 0。日志已打印实际响应中的账户公开信息、Cookie 说明及 Steam 跳转和密码说明三个段落；本次 SHA-256 为 `eedd9e288d40609fca315375d373dd3ccfb001cb14f31b2527982c5850f84d48`。输出改动的 13 项验收回归通过。

## 本机验收

正式入口在北京时间 20:16:15 收到：

- POST `https://steamdb.info/login/`，HTTP 200。
- HTML 35,681 字节，标题 `Sign In · SteamDB`。
- `Sign in through Steam` 控件与六项正文检查全部符合截图。
- 新 profile，点击 1 次，耗时约 29.74 秒，进程退出码 0。
- 正文 SHA-256：`32b3c4277b8f4e2240e9a197193425dfd119c1db2f4e89a92de4c28f0f27f1de`。

最终原生扩展重建后，在 20:22:23 再次使用正式入口和新 profile 独立通过：POST 200、35,681 字节、同一标题及全部正文检查通过，点击 1 次，耗时 21.07 秒，退出码 0。第二次 SHA-256 为 `02a848e8793394fb59993ecc76f8f2a9562ebed60bda3d80416a4bb48b11d398`。

最终针对性回归 125 项通过；项目 `tools/check_policy.py` 的 6/6 门禁全部通过。现有 WebSocket 测试发出弃用提示，未出现测试失败。

页面包含动态值，后续哈希可以不同。验收要求当前顶层 GET/POST 文档导航返回目标地址、HTTP 200、HTML 内容类型及登录页正文；单独取得 clearance 不算成功。

## 框架配置和修复

本例使用 `Browser(gpu_enabled=False)`。此通用选项使页面、iframe 和 Worker 的 WebGL/WebGPU 上下文与 WebGPU adapter 请求一致返回不可用，保留 CPU Canvas 2D。此前模拟 GPU 的部分参数与实际功能不一致。本次先用分析实验验证不可用配置，再正式接入框架并重新运行交付入口，没有向请求填入浏览器捕获的 GPU 回答。

框架同时修复了流转发、OPFS 与 Worker 同步文件访问、COOP/COEP 隔离状态、内存统计快照、CSP/Trusted Types 执行、原生控制台后端、权限及来源策略、输入框长度缺省值、只读计算样式映射和基础 SVG 文字测量。原生扩展已使用现有 V8 15.4.68 在本机重建；迁移机器时需按项目构建说明重建原生扩展。

这些是针对性的兼容实现，不表示全部 Web API 已达到完整 Chromium 一致性。离线测试和线上结果分别验收。

## 对比之前为什么能通过

最后验证到的关键切换是统一关闭 GPU 上下文与 adapter：前面的通用 API 修复已叠加时，默认模拟 GPU 配置仍出现 `600010`；改为 GPU 不可用后，分析实验首次取得目标 POST 200，随后正式入口使用同一通用配置在两个新 profile 中独立通过。原实现的 WebGPU adapter 缺少后续设备能力，模拟 WebGL 的渲染与暴露参数也存在差异。服务端没有公开具体拒绝字段，因此这能证明该切换在当前修复组合中的效果，不能证明 GPU 是全部失败的唯一原因，也不能证明其他每项修复都分别必需。

| 改动 | 修复前 | 修复后与证据 |
| --- | --- | --- |
| GPU 能力一致性 | 暴露模拟 GPU 参数和不完整 adapter | 页面、iframe、Worker 一致返回 GPU 不可用；分析实验及两次正式入口线上通过 |
| CSP / Trusted Types | 受策略约束的字符串赋值与 eval 未执行相应限制 | 相同脚本的三个检查位从 0 变为 Chrome 的 7，相关状态一致 |
| 原生控制台 | 缺少浏览器使用的控制台后端，部分格式转换和计数未执行 | 使用 V8 原生控制台后端；探测调用结果数组从 62 项补到 90 项，内容与 Chrome 一致 |
| OPFS 与 Worker | 文件系统未实现；Worker 同步访问接口曝光缺失 | 实际目录、文件、写入、提交及同步访问；存储探测不再报实现错误 |
| ReadableStream.pipeTo | 接收者检查和流转发语义有差异 | 修复锁、背压、完成及错误传播；对应原生方法检查一致 |
| 隔离、计时和内存 | 忽略 COOP/COEP，内存字段逐次读取实时值 | 隔离状态、SharedArrayBuffer、计时精度按上下文决定；内存使用缓存快照 |
| 来源和权限策略 | 跨域 iframe 仍携带来源；响应头禁用的部分权限返回 prompt | 遵守 same-origin 来源策略及明确的权限禁用；请求头与文档来源回归通过 |
| SVG / 样式 / 输入框 | 文字测量缺失、计算样式映射缺失、输入长度默认值不正确 | 用本机字体整形测量 SVG 文字，补齐映射与缺省值；基础用例通过 Chrome 对照 |
| 成功判定 | 曾仅凭 clearance 判定；此前还遗漏了 POST 成功文档 | 必须取得真实顶层 GET/POST 登录文档的 HTTP 200 与六项正文；这是验收修复，本身不等于求解修复 |

## 分析边界

用户更新的 `steamdb.info.har` 共 31 条请求，记录普通浏览器通过 POST 登录文档取得目标页面，用作成功链对照。MCP 采集、Chrome 同字节离线执行和 cfsolve 源码阅读仅用于分析。私有分析材料位于工作区 `artifacts/steamdb-analysis-20261118/`，未进入程序运行路径。

正式入口不导入参考项目，不读取 HAR，不加载保存的 Cookie、token、探测回答或登录 HTML，不调用自动化浏览器。最终结果来自本框架当次线上请求。
