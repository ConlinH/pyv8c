# pyv8c 基准

本目录测量框架自身的性能：原生桥接、执行调度、Web API、样式布局、JS 计算、启动装配、页面生命周期，以及同步/异步两套公开入口。**唯一入口是 `benchmark.py`**；`cases/` 下的模块是它的测量后端，由子进程逐个调用，不单独作为命令行工具使用。运行前先安装本仓库的 `pyv8c`（含已构建的原生扩展），从仓库根目录执行。

## 快速开始

```powershell
python benchmarks/benchmark.py --list                      # 列出全部用例
python benchmarks/benchmark.py --runs 5 --output "$env:TEMP/run.json"   # 跑一遍并保存
python benchmarks/benchmark.py --record                    # 用当前环境重录 perf_baseline.json
python benchmarks/benchmark.py --runs 5 --output "$env:TEMP/run.json"   # 与基线比较
```

`--list` 打印所有可用用例名。`--cases` 接受逗号分隔的子集；不给则跑全部。默认会把本轮结果与 `perf_baseline.json` 比较：中位数和最小值同时超过 10% 才判为回退；Python 版本或 workload 标识不同则拒绝比较。`--output` 把样本、源码摘要和二进制摘要写入 JSON，不改变基线。`--baseline` 可指定另一份基线用于 A/B 对照。

绘图需单独安装 matplotlib（`python -m pip install matplotlib`）。`benchmark` extra 安装进程测量用的 psutil。原生绑定层的独立微基准仍在 `pyv8/benchmarks/`。

## 用例清单

`--list` 是权威来源；下面按族说明口径。每个用例名前缀对应一个测量后端。

| 族 | 用例 | 测量后端 | 口径 |
| --- | --- | --- | --- |
| 启动/装配 | `startup`、`cold_compile`、`page_cycle`、`startup_offline`、`startup_default` | `benchmark.py`、`cases/startup.py` | 导入到首个可用页面、全接口编译、建页关闭周期 |
| 宿主调用 | `evaluate`、`host_call`、`js_loop`、`stub_call`、`hook_call`、`abort_call` | `benchmark.py` | evaluate 往返、JS 调 Python、stub/hook/实现的成员调用成本 |
| HTML/DOM | `dom_build`、`dom_parse`、`html_fragment`、`html_attributes`、`dom_python_build`、`dom_query`、`event_dispatch` | `benchmark.py` | 文档解析、innerHTML、属性解码、owner 线程建树、查询与事件 |
| 资源/内存 | `resource_batch`、`resident_pages`、`lifecycle_pages` | `benchmark.py`、`cases/lifecycle.py` | 批量子资源、活动页内存增量、页面创建/关闭泄漏验收 |
| 本地 HTTP | `network_host_curl`、`network_host_cronet` | `cases/network.py` | 回环 HTTP 串行延迟、8 页并发尾延迟、等待线程数、关闭和对端断开 |
| 页面资源 / Worker HTTP | `network_resources_curl`、`network_resources_cronet` | `cases/network.py` | 页面连续 12 次 Fetch 的均值与 Worker 并发 12 次 Fetch 的总耗时 |
| HTTP 分段 | `network_stages_curl`、`network_stages_cronet` | `cases/network.py` | 回环保持连接，Fetch 和 XHR 分别读取 2 字节和 1 MiB。JSON 输出含请求准备、调度准入、传输、owner 回投、Realm 结果和快照转换各段样本 |
| Cronet 完成通知 | `network_cronet_completion` | `cases/network.py` | 直接调用 Cronet SDK，分别测量响应头、首块、末块至 EOF、流的关闭耗时以及整包响应耗时。流式测试分立即回包和延迟 50 ms 写出末块两组 |
| 执行调度 | `execution_*`（dispatch/routed/evaluate/result/native/script/idle/ua/dom/concurrent/timer_load） | `cases/execution.py` | 队列往返、owner 内调度、标量/对象求值、原生执行、空闲 CPU、协作并发、timer 干扰 |
| 原生桥接 | `bridge_*`（callback/method/getter/setter/参数/python_call/exec_cached/snapshot 系列） | `cases/native_bridge.py` | 函数/IDL 成员、对象与字符串参数、JS 回调、缓存脚本、对象快照 |
| Web API | `web_*`（getter/属性读写/转换/观察/id 查询/选择器/matches/事件/mixed） | `cases/web_api.py` | 128 组 DOM 节点上的分项与混合操作 |
| 样式布局 | `style_*`（layout/computed/cached/universal/cold/shadow） | `cases/style.py` | 128 元素 × 300 规则，重排、计算样式、缓存命中、Shadow DOM |
| JS 计算 | `javascript_*`（trig/hoisted_trig/integer/float × fresh/retained） | `cases/js_compute.py` | sin/cos、整数与浮点循环，fresh/retained 生命周期，pyv8c 与裸 Context |
| 公开入口 | `entrypoint_sync_*`、`entrypoint_async_*`（evaluate/result/ua/dom/wait） | `cases/entrypoints.py` | 同一负载下同步门面与异步门面的延迟 |

## 命令速查

各测量族通过 `--cases` 选择，公共参数 `--runs` 控制重复次数；每族另有迭代/预热参数，默认值见 `--help`。

```powershell
# 执行调度与协作并发
python benchmarks/benchmark.py --cases execution_evaluate,execution_concurrent,execution_timer_load --runs 5 --output "$env:TEMP/exec.json"

# 原生桥接（A/B：先 --record 记录 before，再用 --baseline 比较 after）
python benchmarks/benchmark.py --cases bridge_snapshot,bridge_snapshot_graph,bridge_exec_cached --runs 5 --record --baseline "$env:TEMP/before.json"
python benchmarks/benchmark.py --cases bridge_snapshot,bridge_snapshot_graph,bridge_exec_cached --runs 5 --baseline "$env:TEMP/before.json" --output "$env:TEMP/after.json"

# Web API 与 owner 线程剖析
python benchmarks/benchmark.py --cases web_selector,web_attribute_write --runs 5 --output "$env:TEMP/web.json"
python benchmarks/benchmark.py --cases web_selector --runs 1 --web-profile-dir build/web-api-profiles
python -c "import pstats; pstats.Stats('build/web-api-profiles/selector.pstats').strip_dirs().sort_stats('cumulative').print_stats(20)"

# 样式布局
python benchmarks/benchmark.py --cases style_layout,style_computed,style_cached,style_shadow --style-iterations 12 --runs 5 --output "$env:TEMP/style.json"

# JS 计算：pyv8c 页面 与 裸 Context 两套引擎
python benchmarks/benchmark.py --cases javascript_integer_retained,javascript_float_retained --javascript-engine pyv8c --runs 5 --output "$env:TEMP/js-page.json"
python benchmarks/benchmark.py --cases javascript_integer_retained,javascript_float_retained --javascript-engine raw --runs 5 --output "$env:TEMP/js-raw.json"
python benchmarks/benchmark.py --cases javascript_integer_retained --javascript-engine pyv8c --javascript-warmup 100 --runs 5 --output "$env:TEMP/js-warm100.json"

# 同步 / 异步公开入口对照
python benchmarks/benchmark.py --cases entrypoint_sync_evaluate,entrypoint_async_evaluate --runs 5 --output "$env:TEMP/entry.json"

# WebSocket 本地回环：每条连接交替发送文本和二进制消息
python -m benchmarks.cases.websocket --connections 1 32 128 --messages 20 --repeat 3

# 页面生命周期验收与内存曲线（独立报告，供绘图）
python benchmarks/cases/lifecycle.py --count 1000 --output "$env:TEMP/lifecycle-report.json"
python benchmarks/plot_results.py --input "$env:TEMP/lifecycle-report.json" --output-prefix "$env:TEMP/lifecycle"
# 或纳入统一基准套件（只计时与保留检查，不产出绘图样本）
python benchmarks/benchmark.py --cases lifecycle_pages --lifecycle-count 1000 --runs 1 --output "$env:TEMP/lifecycle.json"
```

`--javascript-engine` 可选 `pyv8c`（Browser 页面）、`raw`（裸 Context）及 `raw_current`、`raw_incumbent`、`raw_owned`（裸 Context 的观察/线程绑定变体）。`--gc-mode disabled` 作用于 `execution_*` 与 `web_*`；原生桥接项始终用默认 GC。`--entrypoint-api` 与 `--lifecycle-count` 是对应 worker 的内部参数，通常不必手动指定。

## 口径与复现

- 独立的 WebSocket 回环基准在一个进程中输出逐轮 JSON，记录连接就绪时间、消息往返 p50/p95、连接打开时新增的线程和 RSS，以及 Browser 关闭耗时。安装了 `psutil` 时才测量 RSS。
- 每轮在**独立子进程**中测量，避免 V8 热身与内存状态串扰；重负载项（冷编译、内存、JS 计算、原生桥接、生命周期）尤其如此。正式比较在同一机器串行交替运行至少五轮。
- 输出保留源码树 SHA-256、加载二进制 SHA-256、V8 版本、原生构建指纹与 worktree 摘要。DOM 源码与实际加载的 `_native` 构建摘要不符、或 `_pyv8` 与构建记录不符时拒绝测量；需先 `python setup.py build_ext --inplace` 重建。
- `us/crossing`、`us/iteration`、`us/op`、`us/batch` 是整轮耗时除以各自的操作数；延迟样本、p99、最大值保留原批次口径，不要混读。
- 固定次数预热不等于 JIT 层级证明。JS 计算项用 `--javascript-warmup` 调预热，必要时单独运行 `--trace` 检查优化/反优化，诊断日志不作正式样本。
- `perf_baseline.json` 由 `--record` 生成，只存比较所需的聚合字段（`median`/`min`/`unit`/`detail`/`workload`/`runs`），不含逐次延迟数组；含测量时的 Python 版本与 workload 标识；版本或标识变化后旧基线不再可比，需重录。`--output` 保存的历史快照不参与默认比较。
- `lifecycle_pages` 是**验收**用例：任一轮仍有 page/window/Context 被保留即判失败，不只是计时。

## 历史文档

`docs/pyv8/bridge-kernel.md` 与 `docs/pyv8/bridge-refactor-results.md`（及对应英文版）记录的是**历史上的测量方法与实验结果**，其中 `benchmarks/bridge.py`、`benchmarks/results/`、独立 `lifecycle.py` 等路径/脚本已随统一入口重构移除或改名。这些文档按原样保留作为历史证据，不代表当前命令；当前请以本页为准。
