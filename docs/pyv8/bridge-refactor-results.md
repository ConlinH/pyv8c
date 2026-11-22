# 桥接重构结果

**简体中文** | [English](../en/pyv8/bridge-refactor-results.md)

> 本文是历史实验结果，命令与数据均对应旧测量方式。当前统一基准入口见 [benchmarks/README.md](../../benchmarks/README.md)。

## 范围与状态

已实现原生生命周期/资源重构、CPython 3.12 Stable ABI、
vectorcall 回调与访问器、默认 JS 句柄、显式物化、
被动跟踪修复，以及相应的 pyv8c 求值边界变更。
Windows 上的正确性、性能、内存和 wheel 检查均在本地通过。
Linux 构建及 Linux ASan/UBSan 运行在本环境中**未经验证**；
完整的跨平台验收仍有待完成。

本次历史测量使用 V8 15.2.20.1；当前默认值记录在
`pyv8/V8_VERSION`。未保留针对
`preserve_arguments` 或 `_to_python` 的兼容层。参见 [bridge-kernel.md](bridge-kernel.md)
了解接口、所有权、关闭语义与复现命令。

## 构建标识

- 日期：2026-09-18，Windows x64，MSVC 14.44，release `/O2 /GL /MT`。
- 基线来源：`pyv8` 的提交 `45a19de56d12108dbe24b08ffa60359aecbabd1e`，取自原生绑定
  与浏览器框架合并进同一仓库之前；那次合并重写了历史，
  因此该哈希不再可解析。
- 基线源码/二进制快照：`build/bridge-baseline-source.zip`、
  `build/bridge-baseline-pyv8c.zip`、`build/bridge-baseline.pyd`。
- 基线二进制 SHA-256：`5d882a4adcb613abcddf87852c7292473bb1e3cd4177b66c30123fff1695339f`。
- 已测试的就地产物 SHA-256：`1d6e163081108337dad76f51f4f159ba7a39e4cb1d537305a3fd1b05fe5990bd`。
- 改动仍留在工作区中。基准 JSON 中的版本字段
  标识的是父提交；并不表示重构已提交。
- wheel：`dist/bridge/pyv8-2.1.2-cp312-abi3-win_amd64.whl`。
  这是旧双包布局的历史产物，记录于
  2026-10-14；当前发行包是从仓库根构建的
  单一 `pyv8c` wheel。
  其干净构建使用单独的输出目录。其元数据带有
  `cp312-abi3-win_amd64` 标签；唯一的扩展二进制原为 `pyv8/_pyv8.pyd`
  （现为 `pyv8c/pyv8c/pyv8/_pyv8.pyd`）。
  仓库中原有的 Linux `.so` 未在其中重新构建或随其分发。

## 正确性

两个项目的全部测试集中收集，使用相同的就地产物，
并将本地 Python 源码置于 `PYTHONPATH`：

| 解释器 | 结果 | 耗时 |
| --- | --- | --- |
| CPython 3.12.9 | 916 通过，35 个子测试通过 | 100.08 s |
| CPython 3.13.3 | 916 通过 | 64.22 s |
| CPython 3.14.6 | 916 通过，35 个子测试通过 | 65.57 s |

3.13 的 pytest 安装不单独报告子测试。每次合并运行都会
产生两条未知标记警告，因为原生仓库的 pytest 配置
未注册 pyv8c 的 `race` 和 `native` 标记。没有测试失败。
仅原生的验证也通过：332 个测试和 35 个子测试；pyv8c 贡献
584 个测试。日志位于 `build/bridge-python*-tests.log`。

覆盖包括此前失败的两个被动跟踪用例、循环回收、
跨 Context 保留与失效、回调可重入关闭、关闭竞态、
Worker / Inspector 关闭、来源检查、隔离的错误钩子以及
不带 `--expose-gc` 的子进程关闭。新增测试覆盖默认句柄、
无 Proxy 观察的对象标识往返、显式循环物化、
vectorcall 栈/堆参数边界与异常状态生命周期。

解包后的 wheel 还从 `build/wheel-validation` 在
全部三个解释器上加载，执行原生物化以及 pyv8c 页面的创建、
求值与关闭。这些是 wheel 冒烟检查，而非第二套完整测试。

## 性能

`benchmarks/bridge.py` 每个版本使用五个独立进程。每个工作负载
每进程包含五个预热批次和二十个测量批次。以下结果为
各进程中位数的中位数，单位为每批次毫秒。两次运行均使用
CPython 3.12.9，以及同一台机器、同一 V8 与 release 编译器设置。
最终基准在构建和测试完成后运行。

| 工作负载 | 改造前(ms) | 改造后(ms) | 耗时下降 |
| --- | ---: | ---: | ---: |
| Callback, 0 arguments | 1.584 | 0.822 | 48.1% |
| Callback, 1 argument | 2.060 | 0.910 | 55.8% |
| Callback, 4 arguments | 3.226 | 0.985 | 69.5% |
| Callback, 16 arguments | 7.578 | 1.503 | 80.2% |
| Bound method | 4.359 | 1.615 | 63.0% |
| Property getter | 2.746 | 1.484 | 46.0% |
| Property setter | 3.144 | 1.526 | 51.5% |
| Host object roundtrip | 3.735 | 1.947 | 47.9% |
| Exception | 215.297 | 210.696 | 2.1% |
| Trace enabled | 1.439 | 0.656 | 54.4% |
| Local DOM | 3.113 | 2.912 | 6.5% |
| Page create/close | 4.915 | 4.912 | 0.1% |

前九个宿主工作负载的几何平均耗时比为 **0.4453**，
即 **耗时减少 55.5%**，超过 15% 的目标。没有任何
单个被测工作负载出现超过 5% 的回归。页面生命周期基本不变。
这些结果描述的是这些固定工作负载，而非所有应用。异常
用例包含现有的错误报告开销；它并非纯粹的抛出基准。
跟踪与 DOM 批次各有 500 次操作；其他原生批次有 2,000 次。
页面生命周期批次每个创建并关闭一个页面。

原始样本为 `benchmarks/results/before.json` 与
`benchmarks/results/after.json`。这些是历史测量，随测量环境生成，未随仓库分发。

## 内存

独立的 `benchmarks/lifecycle.py` 进程以 20 个页面预热，随后
在常规 chrome 安装下创建并关闭 1,000 个页面。每 100 轮
运行三次 V8 LowMemoryNotification 与 Python GC。**Page、Window 及
原生 Context 的存活计数在全部十个检查点均为零。** Page/Window 使用
弱引用；原生 Context 因其没有弱引用而使用 GC 跟踪的标识。
每批次都会清除观察引用，以避免其累积开销。

私有内存在测量前为 58.4 MiB，第 100 轮为 71.5 MiB，
第 1,000 轮为 75.9 MiB。从第 400 轮起，其范围在 74.8 至 75.9 MiB 之间，
未随页面数量成比例增长。Python 跟踪的分配增量从
第 100 轮的 51.1 KiB 增至第 1,000 轮的 88.6 KiB；最后 500 轮
增加约 7.4 KiB。这些数值包含少量累积的报告与运行时
缓存。此次运行表明页面所有权已释放且私有内存曲线趋平；
它并不断言每个字节或 RSS 都会回到初始值。

原始数据为 `benchmarks/results/lifecycle-after.json`，曲线图为 `lifecycle-after.png`；
两者都是历史测量，未随仓库分发。
可选的 `benchmarks/plot_results.py` 使用 matplotlib 绘制内存曲线。

## 待完成的验证

本地没有可用的 Linux 执行环境，也没有匹配的 Linux V8 构建。
`pyv8/tools/validate_linux.sh` 与 `PYV8_SANITIZERS=address,undefined` 提供了
桥接 sanitizer 的构建/运行路径，但尚未执行。完全插桩的
V8 覆盖还要求 V8 以相同的 sanitizer 配置构建。
同平台的 3.12/3.13/3.14 ABI3 矩阵仅在 Windows 上得到验证。
