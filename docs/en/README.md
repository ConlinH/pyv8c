# Documentation Index

[简体中文](../README.md) | **English**

This collection describes the current implementation and interface contracts. Complete the [project installation](README.en.md#installation) before reading the examples. Commands run from the repository root by default. `pyv8/` is now a build-support directory rather than a separately installable package; the articles under `docs/pyv8/` describe the embedded V8 native layer, and some paths and build commands still follow the former package layout. Check those against the current source and the [development guide](pyv8c/development.md).

| Document | Contents |
| --- | --- |
| [Usage guide](pyv8c/usage.md) | Creating objects, page execution, waiting, environment configuration, input and shutdown |
| [Interface contract](pyv8c/api-contract.md) | Object ownership, sync/async boundaries, timeouts, shutdown and deep queries |
| [Cooperative Browser](pyv8c/cooperative-runtime.md) | Sync/async entry points, same-thread waits and lifecycle |
| [Architecture](pyv8c/architecture.md) | Module layers, binding assembly, ownership and state ownership |
| [Task scheduling](pyv8c/task-scheduling.md) | Execution groups, agents, task sources, microtasks, timers and rendering opportunities |
| [Resource loading](pyv8c/resource-loading.md) | Interceptors, loaders, redirects, caching and document resource lifecycle |
| [Network transports](pyv8c/network-transports.md) | HTTP/WebSocket engines, proxies, cookies and custom transports |
| [Request headers](pyv8c/request-headers.md) | Request scenarios, origins, header generation and transport differences |
| [Windows and same-origin access](pyv8c/window-security.md) | iframes, WindowProxy, origins and cross-origin access |
| [IDL format](pyv8c/idl-format.md) | Interface baselines, iteration metadata, extraction and environment profiles |
| [Debugger](pyv8c/devtools.md) | Inspector, connections, pausing, sub-targets and shutdown |
| [Browser tracing](pyv8c/tracing.md) | Trace configuration, event format, threading constraints and observation boundaries |
| [Development guide](pyv8c/development.md) | Implementation conventions, verification commands, behavior comparison and performance measurement |
| [pyv8 user guide](pyv8/user-guide.md) | Native binding APIs, lifecycle and troubleshooting; builds run from the repository root |
| [Benchmarks](../../benchmarks/README.md) | Unified benchmark entry point, case catalog, measurement methodology and reproducibility |
| [Development tools](../../tools/README.md) | Purpose and operating boundaries of the repository tools |
| [Page behavior comparison skill](../../skills/pyv8c-challenge-parity/SKILL.md) | Locating browser semantic differences using websites or local pages |

Topic files use lowercase English words and hyphens, and the index keeps the name `README.md`. The Chinese collection is the primary version; this English collection mirrors its paths under `docs/en/`. Python, JavaScript, protocol and configuration names keep their real spelling. The four `pyv8/` articles (`architecture`, `bridge-kernel`, `bridge-refactor-results`, `source-layout`) were originally drafted in English; the Chinese collection now carries complete Chinese translations, and this English collection keeps the original English wording.

These documents do not replace source code or tests. When investigating a difference, record the interpreter, native extension, IDL, environment profile, local Chrome and network transport versions together, and do not treat them as one version number.

For the concrete implementation and its limits, see [Web Capabilities and Coverage](pyv8c/web-capabilities.md).
