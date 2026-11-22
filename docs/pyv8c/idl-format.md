# IDL 格式与绑定

**简体中文** | [English](../en/pyv8c/idl-format.md)

[文档索引](../README.md)

## 基线与环境档案

运行时默认读取 `pyv8c/pyv8c/idl/idl.zip`，归档中包含一个 JSON 文档，也可通过 Browser 的 `idl_path` 指定 ZIP 或 JSON。只接受当前格式版本 **10**，包内基线标记 Chromium **154.0.8030.0**；其他格式需重新生成。

环境档案位于 `pyv8c/pyv8c/data/chrome-environment.json`，当前 source 记录的采集浏览器为 Chrome **156.0.8078.80**。省略 `environment` 时，`Browser` 从同一目录读取 `sys.platform` 对应的档案：`darwin` 为 `chrome-environment-darwin.json`。`chrome-environment.json` 仍是部分覆盖的底稿，也是采集器的默认输出。IDL 的来源版本、环境档案版本、原生 V8 版本和测试使用的 Chrome 是不同信息，不能互相推算。环境档案还记录实际采集参数，应随结果一起核对。

`environment` 和 `idl_path` 都省略时，`Browser` 也按 `sys.platform` 选择 IDL。`darwin` 读取 `idl/idl-158.0.8127.36.zip`；文件缺失或其他平台仍读取包内 `idl.zip`。直接 `Idl.load()` 始终读取包内基线 **154.0.8030.0**。原生 V8 版本仍是 15.4.68。

`source.preserved_overrides` 列出本次采集器未采集、但从原档案保留的显式配置；这些值不是本次 Chrome 会话的测量结果。

IDL 描述接口形状和绑定元数据，不包含完整方法实现。接口数量、提取成功以及 navigator 的版本字符串都不能代表浏览器兼容率。

## 文档结构

```json
{
  "idl": {
    "version": 10,
    "source": "tools/extract_idl.py",
    "chromium_version": "154.0.8030.0",
    "report": {}
  },
  "interfaces": {}
}
```

这里的空 interfaces 仅用于展示层级，不能作为有效基线加载。真实集合必须包含有效接口及其继承关系。Window 观察顺序只保存在环境档案的 `bindings.own_order` 中。

| 接口字段 | 职责 |
| --- | --- |
| parent、tag、exposed_in | 父接口、toString tag 和允许的 Realm 类别 |
| constructor | 接口对象暴露、new/普通调用许可及 length |
| template | 原型、全局对象、definition_kind、访问检查和 handler 元数据 |
| members.instance/prototype/interface | 安装到实例、原型和接口对象的成员 |
| constants | 常量名称、值和安装位置 |
| iteration | 独立的同步迭代语义及方法暴露 |

成员包含名称、kind、length、property_flags 或 flags、setter、receiver、cross_origin、side_effect、main_world 等实际绑定所需信息。property_flags 是属性位的整数表示；不能从普通同名操作推导接口迭代能力。

可选的 `install_phase` 区分初始模板安装（省略或 `template`）、后期模板安装（`template_late`）和上下文对象安装（`object`）。提取器从 Chromium 生成的安装函数中记录该信息，原生层据此在每个 Realm 保持原型属性及 `constructor` 的相对顺序。未携带该字段的旧文档按初始模板处理。

template.handlers 分别记录 named、indexed 和 cross_origin 的角色。它们是提取证据，不是 Python callable；绑定编译器再解析到实际宿主处理器。

## 同步迭代

```json
{
  "iteration": {
    "kind": "pair",
    "default": "entries",
    "iterator": true,
    "methods": ["entries", "forEach", "keys", "values"]
  }
}
```

| kind | 数据模型 | 默认迭代方法 |
| --- | --- | --- |
| indexed | length 与数字索引 | values |
| pair | 键值对 | entries |
| set | 同一值作为键和值 | values |

原生 IdlSpec 的职责分别为：`iteration_kind` 选择语义，`array_methods` 指定 indexed 借用的数组方法，`iteration_methods` 指定宿主 pair/set 视图，`iteration_source` 提供宿主数据源。不是每个接口同时填写四项；非法组合会被校验。

pair/set 宿主约定为 `__v8_iteration_source__(self)`，**每次迭代创建独立源**，返回一个无参 next 回调；next 返回 `(key, value)` 或 None。set 返回同一对象两次。indexed 不使用 pair/set 数据源。

数据源按具体接口与 Chrome 对照结果表达实时读取、快照和结束状态，不用统一快照或统一粘滞完成规则掩盖差异。Symbol.iterator 与其默认命名方法必须是同一函数对象；forEach 由原生执行，回调参数为 `(value, key, receiver)`。

async iterable 单独识别为尚未支持，不生成同步迭代配置。缺失宿主迭代源按未实现策略记录，不静默返回空集合。

## 提取与加载

```powershell
python tools/extract_idl.py .\idl-output.zip --chromium-root D:\src\chromium --build-dir D:\src\chromium\src\out\Idl154Fresh --report
```

提取输入为对应版本的 Chromium 生成绑定和源 IDL。上例输出到独立文件，生成目录应按实际环境调整；也可用 `--bindings-dir` 明确绑定目录。版本从源码 VERSION 读取，必要时使用有依据的 `--chromium-version`。仅改版本字符串不能替代对应版本的生成证据。

`pyv8c/idls/` 保存按 Chromium 版本命名的归档，供指定版本的测试或调用方使用；运行时默认读取包内 `idl.zip`，更新版本归档不会自动更新包内基线。重建旧版本时，生成绑定和源 `.idl` 都必须来自目标版本：例如当前源码工作树是 154 时，不能直接把它的源 `.idl` 与 145 的生成绑定混用。可从本地对应 Git 标签准备独立的旧版源码，再指定旧版绑定目录和源码根，并保留版本证据。提取报告中的 `unresolved_signatures` 表示仍未还原的签名数量，即使 `parse_failures` 为空也不代表全部签名已解析。

同步迭代综合数组 intrinsic 安装、SyncIterator、Symbol.iterator 指向和源 IDL 的 iterable/maplike/setlike；partial 和分片合并。证据冲突或无法确定类型应报告接口和文件并失败，不默认降级成数组迭代。

格式 10 不保存 Window 顺序；兼容读取格式 8 和 9，旧归档内顺序字段不参与运行时。其他格式版本会明确拒绝。加载还校验归档安全、条目形状、父接口和继承环等。边车 JSON 与二进制缓存是本地加载缓存，不属于发布基线，按版本和摘要失效。

环境 `bindings` 描述全局暴露、成员省略和方法暴露事实，只限制接口表面，不能覆盖迭代语义。运行时由 IDL 集合生成装配计划，binder 编译 IdlSpec，Realm 的绑定会话完成安装。

## 环境采集

```powershell
python tools/collect_environment.py --chrome "C:\Program Files\Google\Chrome\Application\chrome.exe" --out .\environment.json
```

环境档案格式为 **4**。采集结果的 `bindings.own_order` 保存检查接口之前的 Window 自有属性枚举顺序，同时用于派生暴露集合；不再保存独立的 `bindings.exposed`。宿主属性按该顺序安装，未记录的名称使用稳定的 IDL 次序。未配置绑定档案时警告并使用稳定次序；旧格式和 `bindings.exposed` 不再支持。环境顺序可跨 IDL 摘要使用，但成员省略、迭代和暴露筛选仍受 `bindings.against` 校验。版本差异及可观察顺序差异按 `baseline_mismatch` 汇总；未能区分 V8 内置名称和目标浏览器新增名称时标为待分类，不当作确定缺失。`browser.idl_info["window_order_source"]` 显示 environment 或 fallback。

先输出到独立文件核对 source、启动参数和能力来源，再决定是否更新随包档案。不要默认覆盖基线；采集到的设备、驱动和权限状态不等于框架已经实现了对应硬件能力。
