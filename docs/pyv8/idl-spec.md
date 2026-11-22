# 原生 IDL 绑定声明

**简体中文** | [English](../en/pyv8/idl-spec.md)

本文说明 `pyv8` 项目的原生扩展 `_pyv8` 中的 `IdlSpec` 及成员描述符。它们用于声明 Python 类型如何暴露为 JavaScript 接口，不是 Web IDL 文本解析器，也不自动实现浏览器 API。

## 1. 基本声明

```python
from pyv8c import pyv8

class Example:
    __v8_idl_spec__ = pyv8.IdlSpec(
        constructor_behavior=pyv8.FlagConstructor.kNew,
        has_constructor=1,
        to_string_tag="Example",
        attributes=(
            pyv8.AttributeSpec(name="value", getter="get_value"),
        ),
        methods=(
            pyv8.MethodSpec(name="add", callback="add", length=1),
        ),
        constants_value=(
            pyv8.ConstantSpec(name="READY", value=1),
        ),
    )

    def get_value(self):
        return 42

    def add(self, value):
        return 42 + value
```

原生描述符共有四种：`IdlSpec`、`AttributeSpec`、`MethodSpec`、`ConstantSpec`。它们均为不可变对象，只接受关键字参数，字段通过属性读取。

```python
original = Example.__v8_idl_spec__
updated = original.replace(length=1)
assert original.length == 0
assert updated.length == 1
assert updated.attributes is original.attributes
```

`replace(**changes)` 创建新对象，并重新校验字段及组合约束；未修改的成员可共享。不可变指描述符字段不能重新赋值，不代表回调捕获的数据或回调所属对象被冻结。

不支持 `spec[key]`、`dict(spec)`、字段赋值、删除或位置参数。成员集合必须是对应描述符组成的 **tuple**，不能传 list 或旧式四元组。完整 Python 签名见 [类型声明](../../pyv8c/pyv8c/pyv8/_pyv8.pyi)。

## 2. 接口字段

### 身份、构造与继承

| 字段 | 默认值与类型 | 含义 |
| --- | --- | --- |
| `name` | `None`；字符串或 `None` | 显式接口名称配置 |
| `to_string_tag` | `None`；字符串或 `None` | `Symbol.toStringTag` 配置 |
| `parent_class` | `None`；Python 类型或 `None` | 显式父类型 |
| `length` | `0`；整数 0..2147483647 | 接口函数的参数数量元数据，不执行参数校验 |
| `has_constructor` | `0`；整数 0 或 1 | 构造器相关安装配置，与是否允许调用分开表达 |
| `constructor_behavior` | `0`；整数 0..3 | 允许的调用形式，见下表 |
| `constructor_adapter` | `None`；回调引用 | 构造适配入口，接收 `(cls, args, kwargs)`，返回宿主实例 |
| `definition_kind` | `DefinitionKind.kInterface` | 接口对象的形状 |
| `unconstructable` | `False`；bool | 在构造回调中拒绝构造 |
| `skip_inherit` | `False`；bool | 控制该类型作为父类型时的继承处理 |
| `hidden` | `False`；bool | 控制反射安装成员时是否隐藏该类成员 |

`constructor_behavior` 使用 `FlagConstructor`：

| 常量 | 值 | 调用形式 |
| --- | --- | --- |
| `kDontAll` | 0 | 两种形式都不允许 |
| `kNew` | 1 | `new Example()` |
| `kCall` | 2 | `Example()` |
| `kAll` | 3 | 两种形式都允许 |

构造适配器不是实例上的 `call`。它替代原生层直接调用 Python 类的步骤；`args` 是转换后的 JS 位置参数 tuple，`kwargs` 是构造关键字字典，存在宿主全局对象时包含原生构造路径提供的 `window` 参数。

`definition_kind` 使用 `DefinitionKind`，三种值互斥：

| 常量 | 值 | JS 形状 |
| --- | --- | --- |
| `kInterface` | 0 | 带 prototype 的接口函数 |
| `kIdlNamespace` | 1 | 普通命名空间对象 |
| `kWrapperTypeNoPrototype` | 2 | 不创建 prototype 的接口函数 |

### 成员集合与对象行为

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `attributes` | `()` | `AttributeSpec` 元组 |
| `methods` | `()` | `MethodSpec` 元组 |
| `constants_value` | `()` | `ConstantSpec` 元组 |
| `prototype_late_members` | `()` | 按顺序移到原型末尾的成员名元组，用于体现后期模板成员、`constructor` 与上下文成员的安装顺序；不更改成员描述符 |
| `call` | `None` | 将实例配置为可调用对象的回调 |
| `immutable` | `0` | 原型不可变标志；不是冻结对象属性 |
| `undetectable` | `False` | V8 undetectable 行为，必须同时提供 `call` |
| `code_like` | `False` | 将实例模板标记为 V8 code-like，影响代码求值时的字符串处理 |
| `named_properties_object` | `False` | 使用命名属性对象安装路径 |
| `named_properties_non_masking` | `False` | 命名属性 handler 使用 V8 `kNonMasking`，避免遮蔽既有属性或原型成员 |
| `global_this` | `False` | 当前保存和校验的配置字段；没有独立读取它的原生执行分支，不应据此推断全局对象绑定行为 |

`immutable` 的位值来自 `FlagImmutable`：`instance=1`、`prototype=2`，可按位或组合。它限制对应对象的原型修改，不等价于 `Object.freeze()`。

## 3. 成员描述符

### AttributeSpec：访问器属性

`name` 与 `getter` 必填；`setter` 可省略。

| 字段 | 默认值 | 类型或范围 |
| --- | --- | --- |
| `name` | 必填 | 不含 NUL 的字符串 |
| `getter` | 必填 | 回调引用 |
| `setter` | `None` | 回调引用或 `None` |
| `location` | `FlagLocation.kPrototype` | 0..2 |
| `property_flags` | `V8Attribute.kNone` | 0..7 位掩码 |
| `receiver_check` | `FlagReceiverCheck.kCheck` | 0 或 1 |
| `cross_origin_get_check` | `FlagCrossOriginCheck.kCheck` | 0 或 1 |
| `cross_origin_set_check` | `FlagCrossOriginCheck.kCheck` | 0 或 1 |
| `side_effect` | `SideEffectType.kHasSideEffect` | 0..2 |
| `replaceable` | `False` | bool；可替换属性的安装配置 |

### MethodSpec：方法

`name` 与 `callback` 必填。`length` 是 JS 函数的 `.length`，不是允许参数数量的上限。

| 字段 | 默认值 | 类型或范围 |
| --- | --- | --- |
| `name` | 必填 | 不含 NUL 的字符串 |
| `callback` | 必填 | 回调引用 |
| `length` | `0` | 整数 0..255 |
| `location` | `FlagLocation.kPrototype` | 0..2 |
| `property_flags` | `V8Attribute.kNone` | 0..7 位掩码 |
| `receiver_check` | `FlagReceiverCheck.kCheck` | 0 或 1 |
| `cross_origin_check` | `FlagCrossOriginCheck.kCheck` | 0 或 1 |
| `side_effect` | `SideEffectType.kHasSideEffect` | 0..2 |

两类成员共用以下常量：

- `FlagLocation`：`kInstance=0`、`kPrototype=1`、`kInterface=2`。
- `V8Attribute`：`ReadOnly=1`、`DontEnum=2`、`DontDelete=4`；默认 0，可按位或组合。
- `FlagReceiverCheck`、`FlagCrossOriginCheck`：`kCheck=0`、`kDoNotCheck=1`。
- `SideEffectType`：`kHasSideEffect=0`、`kHasNoSideEffect=1`、`kHasSideEffectToReceiver=2`。

副作用标记是提供给 V8 的声明，不会验证 Python 回调是否真的无副作用。关闭接收者或跨源检查同样不是权限授予机制，调用方需要负责相应接口的正确性。

### ConstantSpec：整数常量

```python
pyv8.ConstantSpec(name="READY", value=1)
```

`name` 必填；`value` 默认为 0，范围为 **-2147483648..4294967295**。它只表示整数常量，不能放入任意 Python 对象。需要暴露动态对象时，使用访问器或向 Context 设置值。

## 4. 回调引用与解析

回调字段接受可调用对象或类成员名称字符串；可选回调接受 `None`。例如 `getter="get_value"` 会在绑定时从宿主类解析该成员。

实例和原型成员的未绑定回调接收 Python 宿主接收者；已经绑定的 Python 方法不会再重复添加接收者。接口位置的回调不自动添加实例接收者。特殊入口（如构造适配器、属性 handler、迭代数据源）遵循各自协议，不能套用普通方法签名。

解析后的回调由模板持有。模板安装后重新给 Python 类的同名成员赋值，不会替换已经安装的显式绑定；要使用新配置，需要建立使用新声明的 Realm/模板。解析不会修改回调函数对象本身。

字符串引用在构造描述符时只做类型检查；成员是否存在、解析结果是否可调用，在绑定解析阶段检查。内部的 resolve 流程不是描述符公开的 Python 实例方法。

## 5. 同步迭代配置

### 四个字段分别控制什么

| 字段 | 默认值 | 职责 |
| --- | --- | --- |
| `iteration_kind` | `IterationKind.none`（0） | 选择 `none`、`indexed`、`pair` 或 `set` 的迭代语义 |
| `array_methods` | 0 | 仅供 indexed 路径使用，选择借用的 V8 Array 内建方法 |
| `iteration_methods` | 31 | 仅供 pair/set 路径使用，选择原生宿主迭代方法 |
| `iteration_source` | `None` | pair/set 的 Python 数据源工厂 |

两个方法掩码使用相同位值，但作用于不同安装路径：

| 位值 | 成员 |
| --- | --- |
| 1 | `Symbol.iterator` |
| 2 | `entries` |
| 4 | `keys` |
| 8 | `values` |
| 16 | `forEach` |

例如 31 发布全部五个成员，1 只发布 `Symbol.iterator`。`ArrayProto.iterator=1`、`ArrayProto.iterable=31` 是 indexed 路径的便利常量。

**当前不是两个掩码相交后决定发布范围。** indexed 只读取 `array_methods`；pair/set 只读取 `iteration_methods`。`iteration_kind=none` 时，默认的 `iteration_methods=31` 也不会启用迭代。

### 合法组合

| kind | 值 | `array_methods` | `iteration_source` | 实际方法掩码 |
| --- | --- | --- | --- | --- |
| `none` | 0 | 必须为 0 | 必须为空 | 不安装迭代方法 |
| `indexed` | 1 | 0..31 | 必须为空 | `array_methods` |
| `pair` | 2 | 必须为 0 | 必须提供 | `iteration_methods` |
| `set` | 3 | 必须为 0 | 必须提供 | `iteration_methods` |

组合错误在构造或 `replace()` 时抛 `TypeError`。即便 pair/set 的方法掩码为 0，也仍要求提供数据源。

### indexed：读取 length 和数字索引

```python
spec = pyv8.IdlSpec(
    iteration_kind=pyv8.IterationKind.indexed,
    array_methods=pyv8.ArrayProto.iterable,
)
```

宿主需要另行提供可读取的 `length` 和数字索引。原生层直接安装 V8 Array 的迭代内建方法；`Symbol.iterator` 使用 `values`。配置不会把宿主转换成真正的 Array，也不会自动为 Python 容器添加数字属性。

### pair/set：每次创建独立游标

`iteration_source` 的协议分为两步：

```text
工厂(receiver) → 可调用游标
游标() → (key, value) 或 None
```

可直接运行的声明示例：

```python
class Pairs:
    __v8_idl_spec__ = pyv8.IdlSpec(
        iteration_kind=pyv8.IterationKind.pair,
        iteration_methods=31,
        iteration_source="__v8_iteration_source__",
    )

    def __init__(self):
        self.items = [("a", 1), ("b", 2)]

    def __v8_iteration_source__(self):
        snapshot = iter(tuple(self.items))

        def next_pair():
            return next(snapshot, None)

        return next_pair
```

这里明确采用快照。若接口需要实时读取，应在独立游标中保存当前位置，并在每次调用时读取宿主当前数据。不能共用一个实例级游标，否则两个迭代器会互相推进。

原生层要求工厂返回可调用对象；游标返回 `None` 表示本次读取结束，非空结果必须能转换为长度为 2 的序列。set 的数据源应返回 `(member, member)`，对象成员的两个位置应引用同一对象。

pair 的默认迭代方法是 `entries`；set 是 `values`。同时发布默认命名方法和 `Symbol.iterator` 时，两者指向同一个 JS 函数对象。

`forEach` 由原生层调用数据源，回调参数依次为 `(value, key, receiver)`，支持 `thisArg`；回调抛错立即终止本次遍历。无需再为这些原生迭代视图重复声明 `MethodSpec`。

### 完成状态、Realm 与释放

原生层不统一锁死游标的完成状态。是否实时读取、是否快照、返回 `None` 后能否因追加数据而继续，由接口数据模型和游标实现决定。例如当前浏览器层的 URLSearchParams/FormData 使用实时游标，FontFaceSet/StylePropertyMap 使用快照；这些选择由 Chrome 对照固定。

数据游标通过既有弱宿主对象机制持有，在迭代器回收或 Context 关闭时释放，不进入普通函数模板缓存。原生入口检查接收者和 Context 状态；同接口的迭代器 `next` 支持跨 Realm 借用，不同接口迭代器和普通对象不能冒充接收者。

Python 异常沿既有桥接异常机制传播；普通 Python 异常不应假定都能由 JS 的 `try/catch` 捕获。本接口不实现异步迭代器。

## 6. 属性拦截与跨源入口

以下字段均为回调引用，而不是启用/禁用的布尔位。`None` 表示未配置，字符串或 callable 表示配置对应入口。

| 分组 | 字段 |
| --- | --- |
| 命名属性 | `name_get`、`name_set`、`name_query`、`name_enum`、`name_del` |
| 数字索引属性 | `index_get`、`index_set`、`index_query`、`index_enum`、`index_del` |
| 跨源访问检查 | `cross_origin_check` |
| 跨源命名属性 | `cross_origin_name_get`、`cross_origin_name_set`、`cross_origin_name_query`、`cross_origin_name_enum`、`cross_origin_name_del` |
| 跨源数字索引 | `cross_origin_index_get`、`cross_origin_index_set`、`cross_origin_index_query`、`cross_origin_index_enum`、`cross_origin_index_del` |

`get/set/query/enum/del` 分别对应读取、写入、属性查询、枚举、删除入口；具体参数和返回约定由对应原生 handler 决定，不是普通 `MethodSpec` 协议。

字段、handler 标识及 Python 协议名称由 [handler_table.def](../../pyv8/src/interfaces/handler_table.def) 统一声明。显式 IdlSpec 应直接填写回调字段；pyv8c 的绑定编译器可以按约定名称发现实现，再填入描述符。

`named_properties_non_masking` 与 `named_properties_object` 是不同配置：前者调整命名 handler 的遮蔽规则，后者选择命名属性对象安装路径。

## 7. Realm 注册和惰性编译

每个 Realm 维护自己的接口声明与模板。同一接口名称只能对应一个 Python 类型；另一个类型占用相同名称会被拒绝。

Realm 的 `lazy_factory` 可返回：

- Python 类：使用该类的声明。
- `(class, {name: spec})`：为相应接口提供完整声明，包括需要预先登记的祖先声明；完整声明替代类声明，不进行逐字段合并。
- `None`：未提供该名称；已有类声明仍可用于后续绑定。

返回未知名称的结果会被记住，避免重复查询；工厂抛异常不算成功答复，后续可以重试。模板已经建立的接口不会被迟到配置替换。

`Context.install(names)` 在全局对象上安装不可枚举的惰性接口访问入口，脚本实际读取时才构建对应接口。通过另一个 Realm 读取该入口，仍在全局 holder 的创建 Realm 中构建，保持构造器和原型的 Realm 身份。

pyv8c 在上层汇总基线元数据、实现声明和 mixin，通过 binder 生成不可变 IdlSpec。JS 父成员保留在原型链上；与 Realm 无关的编译结果可以共享，需要 Realm 的回调另行绑定。BindingSession 在原生注册前准备祖先链，成功后发布状态。`strict_validation` 执行上层编译审查；它不替代原生运行时检查。

## 8. 校验与常见错误

- 未知字段、错误成员类型、非 tuple 成员集合、越界枚举或标志值：构造或替换时失败。
- 所有 bool 字段要求真正的 Python bool；不要用整数 0/1 替代。`has_constructor` 等明确声明为整数的字段除外。
- 字符串字段和回调名称不能含 NUL；回调名称存在性在绑定时检查。
- `undetectable=True` 缺少 `call`：构造时失败。
- pair/set 缺少数据源、indexed 带数据源或其他非法迭代组合：构造时失败。
- 工厂未返回 callable、游标返回错误长度的结果：实际迭代时失败。

没有 `__v8_idl_spec__` 的对象仍走普通 Python 反射和通用桥接协议。跟踪及异常协议是独立的运行时能力，不属于成员描述符。普通反射无法在缺少 Realm 时将任意 Python 对象安装为模板常量；该路径会抛 `RuntimeError`，应改用 Context 值或显式访问器。

## 9. 旧接口迁移

| 旧用法 | 当前用法 |
| --- | --- |
| 旧式 tuple 成员声明 | `AttributeSpec`、`MethodSpec`、`ConstantSpec` |
| `spec[key]` 或修改字典 | `spec.field` 与 `spec.replace(...)` |
| handler 布尔开关 | 对应回调字段的名称、callable 或 `None` |
| 独立 ConstructorConfig | IdlSpec 构造字段；安装 hook 独立处理 |
| `array=...` | `iteration_kind=IterationKind.indexed, array_methods=...` |
| `iterate=...` / `__v8_iterate__` | `iteration_kind=pair/set, iteration_source=...`，回调改为独立游标工厂 |
| 手写 pair/set 视图方法 | `iteration_methods` 控制发布范围，原生实现视图和 `forEach` |

旧字段和旧回调命名不提供兼容别名。将 `iteration_kind` 从 indexed 改成 pair 时，需在同一次 `replace()` 中清除 `array_methods` 并提供数据源，避免生成非法中间配置。

## 10. 实现入口

- [字段结构](../../pyv8/src/interfaces/idl_spec.h)：描述符原生存储布局。
- [字段注册与校验](../../pyv8/src/interfaces/idl_spec.cpp)：默认值、范围、replace、回调解析与 GC 遍历。
- [枚举和标志](../../pyv8c/pyv8c/pyv8/flag.py)：公开常量。
- [同步迭代](../../pyv8/src/interfaces/iterable.cpp)：indexed、pair、set 安装及执行。
- [模板安装](../../pyv8/src/interfaces/pyclass.cpp)：成员、继承和对象模板配置。
- [Python 类型声明](../../pyv8c/pyv8c/pyv8/_pyv8.pyi)：公开签名和只读字段。
