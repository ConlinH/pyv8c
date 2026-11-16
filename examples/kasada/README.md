# Kasada：OTTO banana 搜索案例

## 用途

请求 https://www.otto.de/suche/banana/，使用 pyv8c 本地执行当次挑战 SDK，
取得搜索页 200 响应，整理商品列表并通过 Loguru 打印前 10 个。
运行结果仅打印到终端，不创建输出目录，不保存 HTML、JSON、SDK 或日志文件。

## 快速运行

在仓库根目录 G:\pyv8env\pyv8c 执行：

```powershell
..\.venv\Scripts\python.exe -X utf8 -B examples\kasada\otto.py
```

指定代理：

```powershell
..\.venv\Scripts\python.exe -X utf8 -B examples\kasada\otto.py --proxy http://127.0.0.1:6696
```

命令中的 `-B` 禁止 Python 生成字节码缓存。
需要当前仓库源码和匹配的原生扩展；本次环境为框架提交 581a460、V8 15.4.68。
Python 依赖包括仓库声明的 `loguru` 和 `curl_cffi`。
运行不需要真实浏览器或 MCP 服务。

## 请求流程

1. 首次 GET 搜索页，收到 HTTP 400 和挑战 HTML。
2. 下载并在本地 V8 中执行 HTML 指向的当次原始 ips.js。
3. SDK 生成参数并 POST /tl，请求携带 x-kpsdk-ct、x-kpsdk-dt、x-kpsdk-im。
4. /tl 返回 HTTP 200、{"reload":true}，响应头包含 x-kpsdk-cr、ct、r、st。
5. SDK 消费响应、延续当前会话并重载搜索页，取得 HTTP 200。
6. 在内存中从本次真实响应 HTML 提取商品，打印总数和前 10 条。

## 日志

沿用其他案例的 Loguru 格式：HH:mm:ss | LEVEL | 内容，输出到 stderr。

- INFO：请求路径、响应状态与长度、SDK 参数头长度、商品、最终状态和耗时。
- SUCCESS：/tl 返回 reload:true、搜索页 200 且成功提取商品。
- ERROR：网络、挑战或商品解析失败；最终失败退出码为 1。

输出示例：

```text
17:50:10 | SUCCESS | /tl HTTP 200，响应 {"reload": true}
17:50:11 | SUCCESS | 搜索页 HTTP 200，商品提取成功
17:50:11 | INFO    | 共整理 120 条商品，按页面顺序打印前 10 条
17:50:11 | INFO    | 商品 02: HappySeed Blumensamen Große Essbanane Samen (Musa x paradisiaca) - Exotische Banane [广告] | 4.90 EUR
```

每条商品分两行打印：名称、广告标记、价格及起售价标记；商品 ID、规格 ID 和完整链接。
不在终端输出挑战令牌的完整值。

## 商品整理规则

- 按搜索页卡片出现顺序排列，保留广告位置并标记 [广告]。
- 按 variation_id 去重；没有规格 ID 时使用 product_id。
- 解码商品链接的 base64-href，补全为绝对 URL，并还原名称中的 HTML 实体。
- retail-price 和 suggested-retail-price 按分换算为两位小数，避免二进制浮点金额误差。
- 首屏完整卡片包含价格、品牌和图片；后续惰性占位卡片仍提取名称、ID 和链接。
  HTML 未提供的价格、品牌和图片不推测：价格与图片为 null，品牌为空字符串。

## 运行结果

响应内容和整理后的商品只在内存中处理，终端打印前 10 个商品及最终汇总。
不提供 `--output` 或 `--save-sdk` 参数。案例仅分发本 README 和 `otto.py`。

退出码 0 表示取得搜索页 200 业务 HTML 并提取到商品；失败为 1。
success 与 challenge_verified 分别记录，首次直接收到 200 不会伪称执行过挑战。

## 可选参数

| 参数 | 说明 |
| --- | --- |
| `--proxy` | 默认 `http://127.0.0.1:6696` |
| `--timeout` | 默认 65，等待预算，单位秒，必须大于 0 |
| `--url` | 默认 banana 搜索页，本次验收仅覆盖该地址 |
| `--environment` | 可选框架环境 JSON；本次验收未使用额外配置 |

`--timeout` 是页面结果的等待预算，HTTP 单次请求超时为 25 秒，浏览器清理也需要时间，
因此该参数不是整个进程的硬退出时限。

## 实测记录（2026-11-16）

- 初始流程版：5 个独立新会话 5/5 通过 400 → /tl reload:true → 搜索页 200。
- 商品解析：对上述 5 份真实 HTML 分别提取 120 条商品，前 10 条名称、价格及链接完整；
  每页 18 条完整卡片的价格与页面 JSON-LD 一致，共核对 90 条。
- 日志版线上复测共 2 次：首次 GET 遇到网络 LoadFailure；随后完整通过挑战，
  获得 583670 字节的 200 页面，提取 120 条商品并打印前 10 条，耗时 6.512 秒。
- 仅打印版复测：完整通过 400 → /tl reload:true → 搜索页 200，提取 120 条商品并打印前 10 条，
  耗时 6.242 秒，退出码 0；运行前后案例目录的文件清单、文件大小及修改时间均未变化。
- 移至 `examples/kasada` 后提交前复测：完整通过上述请求链，搜索页为 584530 字节，
  提取 120 条商品且恰好打印前 10 条，耗时 5.591 秒，退出码 0；
  运行前后案例目录的文件清单、SHA-256 和修改时间均未变化。

初始流程版和日志版属于历史验证，相关运行文件不随案例分发。

## 实现边界

每次创建独立 Profile，执行当次未修改的 SDK。运行不读取 HAR、历史 Cookie、
旧请求体或已保存的 SDK，不调用 Playwright、Selenium、Camoufox 或 MCP。
本地 `www.otto.de.har` 仅作分析参考，由现有 `.gitignore` 排除，不随案例提交；运行时不会生成响应或缓存文件。
本次结果不代表站点改版或网络环境变化后的长期通过率。
