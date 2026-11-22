# 第三方声明（Third-party notices）

本文件记录已识别的非本项目原创材料及其许可与版权声明。发行物的完整静态依赖清单尚待按平台和实际构建输入核对，本文件不代表该审计已完成。

- **本项目自研的 Python 与 C++ 源码**采用 [MIT License](LICENSE)，版权归 `Copyright (c) 2026 ConlinH and pyv8c contributors`。
- 本文件、`LICENSE` 和 `third_party_licenses/` 中的许可原文通过 `pyproject.toml` 进入 wheel，并通过 `MANIFEST.in` 进入 sdist。
- MIT 只要求保留版权与许可声明；但 **V8 与 Chromium 的 BSD-3-Clause 明确要求二进制分发时在随附文档或其他材料中复制版权声明、条件列表与免责声明**，因此本文件必须随 wheel 一同分发，不可只放在网页上。
- 下文的版本号来自仓库根的 `CHROMIUM_VERSION` 与 `pyv8/V8_VERSION`；使用外部 V8 或自定义 ICU 数据构建时，必须另行核对实际版本及其许可。Python 依赖以实际安装环境为准。

> 本文件用于履行署名与告知义务，不构成法律意见。

---

## 1. V8 JavaScript 引擎

- **组件**：V8 静态库（`libv8_monolith` / `v8_monolith.lib`），由 `pyv8/v8_build.py` 构建并静态链接进 `pyv8c.pyv8` 原生扩展。
- **版本**：15.4.68（见 `pyv8/V8_VERSION`）。
- **许可**：BSD 3-Clause "New" or "Revised" License（`BSD-3-Clause`）。
- **版权**：Copyright 2014, the V8 project authors. All rights reserved.
- **上游原文**：<https://chromium.googlesource.com/v8/v8.git/+/main/LICENSE>
- **本地原文**：[V8-LICENSE.txt](third_party_licenses/V8-LICENSE.txt)、[STRONGTALK-LICENSE.txt](third_party_licenses/STRONGTALK-LICENSE.txt)（V8 15.4.68 源码树）。

V8 的 LICENSE 同时声明，以下外部维护库不适用上述条款、各自保留自己的许可：

| 组件 | 许可 |
| --- | --- |
| PCRE 测试套件（`test/mjsunit/third_party/regexp-pcre/regexp-pcre.js`） | 文件内嵌版权与许可（University of Cambridge / Google Inc.） |
| Layout tests（`test/mjsunit/third_party/object-keys`） | BSD-3-Clause（Apple Computer, Inc.） |
| Strongtalk assembler（部分 `assembler-*.cc/.h`） | BSD-3-Clause（Sun Microsystems Inc.） |
| Valgrind client API header（`src/third_party/valgrind/valgrind.h`） | BSD 许可 |
| Wasm C/C++ API headers（`third_party/wasm-api/wasm.{h,hh}`） | Apache License |

V8 源码树各子目录下的 `LICENSE` 文件包含更细的第三方信息，重新构建 V8 时应一并核对。

**BSD-3-Clause 条件与免责声明（V8 LICENSE 主体）**：

> Redistribution and use in source and binary forms, with or without modification, are permitted provided that the following conditions are met:
>
> 1. Redistributions of source code must retain the above copyright notice, this list of conditions and the following disclaimer.
> 2. Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the following disclaimer in the documentation and/or other materials provided with the distribution.
> 3. Neither the name of Google Inc. nor the names of its contributors may be used to endorse or promote products derived from this software without specific prior written permission.
>
> THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

---

## 2. Chromium / Blink Web IDL 基线

- **组件**：`pyv8c/pyv8c/idl/idl.zip` 及 `idls/`、`docs/` 中由 Chromium Web IDL 定义生成的接口规格；`CHROMIUM_VERSION` 记录基线版本。
- **基线版本**：Chromium 154.0.8030.0。
- **许可**：BSD 3-Clause "New" or "Revised" License（`BSD-3-Clause`）。
- **版权**：Copyright 2015 The Chromium Authors；其他文件可能携带各自的版权声明。
- **上游原文**：<https://chromium.googlesource.com/chromium/src/+/main/LICENSE>
- **固定版本原文**：[CHROMIUM-LICENSE.txt](third_party_licenses/CHROMIUM-LICENSE.txt)，来自 Chromium 154.0.8030.0。

这里的 IDL 数据是从 Chromium/Blink 接口定义提取、整理出的规格信息；保留上游版权和许可声明供下游核对。

### Blink 音频压缩器

动态压缩器实现位于 Rust 扩展 `pyv8c/native/src/audio/compressor.rs`，改写自 Chromium Blink 的 `third_party/blink/renderer/platform/audio/dynamics_compressor.cc`，版权为 Copyright (C) 2011 Google Inc.；其 BSD 许可条款含 Apple 的特定名称和免责声明，与 Chromium 顶层文本不同。完整原文见 [BLINK-AUDIO-LICENSE.txt](third_party_licenses/BLINK-AUDIO-LICENSE.txt)，该源码文件顶部也保留了许可头。旧的 Python 实现 `pyv8c/pyv8c/web/bom/audio_compressor.py` 已移除。

---

## 3. ICU 数据文件

- **组件**：`pyv8c/pyv8c/pyv8/icudtl.dat`，随 wheel 分发的 ICU 数据。
- **许可及版权**：固定 V8 15.4.68 构建树中 ICU 78.2 的完整文本见 [ICU-LICENSE.txt](third_party_licenses/ICU-LICENSE.txt)。
- **上游原文**：<https://github.com/unicode-org/icu/blob/main/icu4c/LICENSE> 与 <https://www.unicode.org/license.txt>

该数据文件由 V8 构建链带入；外部数据或不同构建可能使用其他版本，应携带该构建实际使用的许可文件。

---

## 4. Chrome 运行时环境档案

- **组件**：`pyv8c/pyv8c/data/chrome-environment.json`（schema 4），记录从本机 Chrome 156.0.8078.80 采集的接口与属性观测值，用于决定 Window 自有属性顺序等环境行为。
- **性质**：对公开运行时行为的事实性观测数据，不包含 Chrome 的实现代码。
- **注意**：`Chrome`、`Chromium`、`V8`、`Google` 均为各自权利人的商标或标识；本项目与 Google 及 Chromium 项目无关联、未获背书。上述 BSD-3-Clause 第 3 条禁止借其名义为衍生产品背书。

---

## 5. Python 依赖

以下依赖由使用者通过 `pip` **单独安装，不打包进 pyv8c 的 wheel**；列出它们是为了便于下游做完整的许可证审计。SPDX 标识取自各包的分发元数据（无 `License-Expression` 的包按 classifier 判定），版本为 `.venv315` 中核对到的版本，实际以锁定的安装环境为准。

### 运行时依赖

| 包 | SPDX | 核对版本 |
| --- | --- | --- |
| loguru | MIT（classifier） | 0.7.3 |
| curl_cffi | MIT | 0.17.0 |
| websockets | BSD-3-Clause | 17.4 |
| uharfbuzz | Apache-2.0 | 0.57.0 |
| uniseg | MIT（classifier） | 0.10.1 |
| tldextract | BSD-3-Clause | 5.4.0 |
| idna | BSD-3-Clause | 3.20 |

### 可选依赖

| 包 | SPDX | 核对版本 | 说明 |
| --- | --- | --- | --- |
| cycronet | MIT | 144.5.2 | 可选 Cronet 传输后端 |
| Pillow | MIT-CMU | 13.0.0 | Canvas 像素后端 |
| comfy-angle | BSD-3-Clause（ANGLE）/ MIT（Electron 组件） | 0.1.1 | 实验性 WebGL 后端；wheel 内含 `electron-LICENSE` 与 `LICENSES.chromium.html`，以其为准 |
| playwright | Apache-2.0 | 1.64.1 | 仅 `reference` extra，用于本机 Chrome 对照 |

### 仅开发 / 测试

`pytest`（MIT）、`pytest-timeout`、`pytest-repeat`、`pytest-xdist`、`cryptography`（Apache-2.0 OR BSD-3-Clause）、`psutil`（BSD-3-Clause）、`setuptools` / `wheel`（MIT）等，仅在构建与测试时使用，不随发行包分发。

依赖版本变化后请用如下命令重新生成清单，不要手工维护：

```bash
python -m pip install pip-licenses
pip-licenses --format=markdown --with-urls --with-license-file
```

---

## 6. 发行前待完成的第三方审计

统一原生扩展的音频组件使用 PyO3 0.27.2、RustFFT 6.4.1 及其运行时依赖。扩展采用 Chromium 的 FFT 数据布局，对应的上游许可证在 `third_party_licenses/CHROMIUM-LICENSE.txt`。所链接的 Rust crate 许可证文本在 `third_party_licenses/AUDIO-RUST-CRATES-LICENSES.txt`。构建脚本使用 `cc` 编译 DOM C++ 源码，并使用 `sha2` 记录源码摘要。这两个 crate 仅在构建期间使用。依赖升级后应重新核对并更新许可证材料。

V8 的 monolith 和各平台链接输入还可能包含其自身的第三方代码。特别是 Windows 构建会链接 Temporal 的 Rust 库及其传递依赖；不同平台、GN 参数、V8 来源和 ICU 覆盖均可能改变组成。上述文件不是完整物料清单，不能仅凭通过基础检查就认定 wheel 可发布。

对本机 V8 15.4.68 Windows x64 构建的 `obj/mksnapshot.ninja` 进行初步检查，发现 Abseil、Highway、ICU、simdutf、zlib 及 `inspector_protocol` 的构建输入；链接记录还列出 `temporal_capi`、`temporal_rs`、`diplomat_runtime`、ICU4X 和其他 Rust 库以及 Rust 标准库。构建工具的输入并不一定全部进入最终 wheel，反过来这份记录也不能证明已覆盖所有目标或平台；它只是后续审计的起点。

发行者需根据每个平台的实际编译及链接依赖生成组件清单，核对各自完整许可证和版权声明，将必要文本随相应 wheel 分发，并记录版本及来源。完成这一审计后才能确定覆盖整个 wheel 的 SPDX 表达式；当前包元数据有意不声明发行物级表达式。

## 7. 分发时的注意事项

1. 保留本仓库 `LICENSE` 中的 MIT 版权与许可声明。
2. 保留本文件和 `third_party_licenses/` 中的许可原文。
3. 若自行构建并分发含有 V8 / ICU / ANGLE 二进制的 wheel，同时携带对应上游组件附带的 `LICENSE` 文件。
4. 不使用 Google、Chromium、V8 或 Chrome 的名义为衍生产品背书，也不暗示本项目为其官方产品。
