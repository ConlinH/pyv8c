# 文档索引

**简体中文** | [English](en/README.md)

本文档集描述当前实现和接口契约。阅读示例前请完成[项目安装](../README.md#安装)。命令默认从仓库根目录执行。`pyv8/` 现为构建支持目录，不再是独立可安装包；`docs/pyv8/` 下的专题文档描述内置 V8 原生层，部分路径和构建命令沿用旧包结构，阅读时请对照当前源码与[开发指南](pyv8c/development.md)。

| 文档 | 内容 |
| --- | --- |
| [使用指南](pyv8c/usage.md) | 创建对象、页面执行、等待、环境配置、输入和关闭 |
| [接口契约](pyv8c/api-contract.md) | 对象归属、同步与异步边界、超时和关闭 |
| [协作式 Browser](pyv8c/cooperative-runtime.md) | 同步/异步入口、同线程执行、等待与生命周期契约 |
| [架构设计](pyv8c/architecture.md) | 模块分层、绑定装配、所有权和状态归属 |
| [任务调度](pyv8c/task-scheduling.md) | 执行组、Agent、任务源、微任务、定时器和渲染机会 |
| [资源加载](pyv8c/resource-loading.md) | 拦截器、加载器、重定向、缓存及文档资源生命周期 |
| [网络传输](pyv8c/network-transports.md) | HTTP/WebSocket 引擎、代理、Cookie 和自定义传输 |
| [请求头](pyv8c/request-headers.md) | 请求场景、来源、头生成及传输差异 |
| [窗口与同源访问](pyv8c/window-security.md) | iframe、WindowProxy、origin 和跨源访问 |
| [IDL 格式](pyv8c/idl-format.md) | 接口基线、迭代元数据、提取和环境档案 |
| [调试器](pyv8c/devtools.md) | Inspector、连接、暂停、子目标和退出 |
| [运行跟踪](pyv8c/tracing.md) | 跟踪配置、事件格式、线程约束和观测边界 |
| [开发指南](pyv8c/development.md) | 实现规范、验证命令、行为对照和性能测量 |
| [pyv8 使用指南](pyv8/user-guide.md) | 原生绑定的 API、生命周期与故障排查；构建以仓库根为准 |
| [基准说明](../benchmarks/README.md) | 统一基准入口、用例清单、性能测量口径与复现方法 |
| [开发工具](../tools/README.md) | 仓库工具的用途与运行边界 |
| [页面行为对照技能](../skills/pyv8c-challenge-parity/SKILL.md) | 以网站或本地页面定位浏览器语义差异 |

专题文件使用小写英文单词和短横线命名，索引保留 `README.md`；正文、标题和示例说明统一使用中文。Python、JavaScript、协议及配置项保留实际名称。

这些文档不替代源码和测试。排查差异时，应同时记录解释器、原生扩展、IDL、环境档案、本机 Chrome 和网络传输版本，不把这些版本当作同一个版本号。

具体实现与限制见 [Web 能力与实现范围](pyv8c/web-capabilities.md)。
