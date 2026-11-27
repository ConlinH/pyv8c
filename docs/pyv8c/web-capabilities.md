# Web 能力与实现范围

**简体中文** | [English](../en/pyv8c/web-capabilities.md)

[文档索引](../README.md)

## 二进制与消息

Request、Fetch、XMLHttpRequest 和 Blob 在所属 Realm 的 owner 上读取 BufferSource。ArrayBuffer、TypedArray 和 DataView 按偏移及长度复制，包含空视图；提交后修改原缓冲区不会改变已提交内容。原生判断不读取页面可改写的 constructor 或 transfer 方法。

Window、Worker 与 MessagePort 共用结构化克隆，支持循环引用、Map、Date、共享缓冲区视图及已实现的 Blob、File、DOMException。接收值由接收 Realm 创建。transfer 列表先验证类型、重复身份和分离状态，再序列化及准备接收值，最后提交缓冲区分离和端口移动。失败不提交框架所有权变更；getter 自身的副作用不回滚。消息内的 MessagePort 必须列入 transfer。SharedArrayBuffer 转移不受支持。

## XMLHttpRequest

Window 支持通过现有同步资源入口执行同步 XHR，沿用源检查、重定向和文档归属校验。同步请求要求默认 timeout 与 responseType，完成状态及 load/loadend 在 send 返回前可见；网络和 CORS 失败抛 NetworkError。Worker 同步 XHR 仍未实现。

send 的 null、undefined 和无参数输入统一为空请求体。成功 open 会取消旧加载与计时器，清空请求头及响应；迟到响应、错误和超时按请求版本隔离。发送中重复 send 或修改请求头会拒绝。

回调内重开、同步重入及 abort 有本机 Chrome 156 对照，保留实测的外层终止事件。另一个 HEADERS_RECEIVED 回调重开异步请求的案例，Chrome 156 会拼接旧、新正文；框架按请求归属保持新正文独立，该差异不作为 Chrome 一致性通过项。以上不代表完整 XHR 上传、二进制响应或文档响应兼容。

## Worker

DedicatedWorker 与 SharedWorker 提供 atob、btoa、structuredClone，Base64 与 Window 共用字符串转换和错误规则。

经典 Worker 的 importScripts 先转换全部 URL，再相对入口脚本解析并依次同步执行；嵌套导入仍相对入口解析。前一项加载或执行失败后不执行后续项。module Worker 调用抛 TypeError。依赖脚本通过独立请求会话加载，详见[资源加载](resource-loading.md)。完整模块加载、CSP、MIME 和 Fetch 预检兼容仍需按场景验证。

## DOM、事件与文档

importNode 创建目标文档所属的节点，支持深浅复制、DocumentFragment 和 SVG 命名空间；Document 与 ShadowRoot 不可导入。脚本的已开始执行状态随克隆保留，移动或重插已执行脚本不会重复执行。

复选框点击监听器看到预激活后的 checked 状态，取消默认动作后恢复 checked 与 indeterminate。宿主输入、元素 click 和 label 转发共用激活流程。click 使用 PointerEvent，srcElement 跟随事件目标，layer 坐标使用当前布局模型。

文档解析结束后先进入 interactive，再执行 defer 队列，完成后派发 DOMContentLoaded。回调关闭页面或替换文档后，旧流程停止推进。

HTML 起始标签复用容错属性边界扫描，属性名或无引号值中的孤立引号不再吞掉后续脚本，script/style 继续按原始文本处理。这不代表完整 HTML5 树构建算法。

body/frameset 的指定事件处理器映射至所属活动文档的 Window，IDL、内联属性及属性修改/删除共享处理器槽。普通元素事件和 addEventListener 保留元素归属。映射至 Window 的 onerror 内联处理器接收五个错误参数；普通 error 事件接收事件对象，并按处理器返回值执行对应取消规则。

## 样式与布局

声明解析识别字符串、URL 和函数内的分隔符。计算入口支持自定义属性继承、var 回退、数值 calc/min/max/clamp、部分颜色及变换计算。层叠规则、字体表、HarfBuzz 测量及样式失效保持统一。only-child 和 only-of-type 按元素兄弟区分。

现有选择器支持转义标点、十六进制标识符和尾部转义空格。样式表复用选择器 AST 分组，避免误拆转义、属性字符串或伪类参数中的逗号；非法普通选择器列表整条忽略。优先级从 AST 计算，is/not 使用参数中的最高值，where 为零。语法、优先级和行内声明可缓存，元素匹配仍读取当前 DOM；属性、class 和 CSSOM 修改后保持实时更新。这些能力不代表完整 Selectors Level 4 或完整 CSS 样式表语法。

getClientRects 返回矩形快照，隐藏或未连接元素返回空列表。目前布局提供单一边框盒，不提供完整行内碎片。支持嵌套 inline-block 的内边距及 min/max-width 约束。Windows 系统色按浅色主题常量验证，不代表深色、高对比度或动态主题兼容。

hasStorageAccess 与 hasUnpartitionedCookieAccess 返回所属 Realm 的 Promise。非活动文档拒绝；有效 origin、安全上下文和祖先站点决定默认状态。当前没有完整存储权限授予流程。

## Window 自有属性顺序

`Object.getOwnPropertyNames(window)` 的宿主属性统一按环境档案 `bindings.own_order` 安装；未观察到的名称按稳定次序追加。可配置的 `Temporal` 和 `WebAssembly` 也按观察顺序安装，并保留原对象与描述符；其他 V8 内置全局属性的位置受上下文创建机制约束，无法满足的顺序会报告。

V8 内置全局段（`Object`、`Function`、`SuppressedError` 等）由本仓库 `pyv8/` 构建使用的 V8 版本决定，不在 Blink 属性顺序数据范围内。当前默认版本为 V8 15.4.68；对照 Chrome 时需核实双方实际 V8 版本、构建选项和 runtime-enabled 接口状态。

## Canvas 与 SVG

Canvas/OffscreenCanvas 的 2D 上下文支持线性、径向渐变和颜色节点、rect 路径、尺寸重置及上下文属性。原生 Skia CPU 后端负责绘制、位图处理以及 PNG、JPEG 和 WebP 编码，字体测量使用现有文本引擎。尺寸赋值重置像素与绘图状态，保留上下文对象和创建选项。框架绘制不等于 Chrome 完整光栅化、色彩管理或 GPU 渲染。

比较 Canvas 导出的 PNG 时应先解码，再比较像素。编码器可为同一像素数据生成不同的压缩字节。

SVGPoint、SVGRect 提供可变单精度数值，SVGMatrix 保留双精度；这些属性拒绝非有限值。getCTM 组合祖先 transform、viewBox 和已实现的 preserveAspectRatio 对齐。getBBox 与 isPointInFill 的验收范围为矩形及矩形后代，不代表完整 path/text 几何和滤镜效果。

## 离线音频完成通知

OfflineAudioContext.startRendering 提供 Promise 结果，并通过队列派发 complete 事件，
支持 oncomplete 和 addEventListener。OfflineAudioCompletionEvent.renderedBuffer 与
Promise 的结果保持同一 AudioBuffer 身份；事件构造器要求有效的 renderedBuffer。
完成事件的顺序与标志已与 Chrome 156 对照，覆盖同步返回后替换 handler 的情形。
完成通知使用独立的 media-element 任务源，并保留文档代次与关闭取消约束。渲染计算本身仍同步执行。
底层渲染仍使用框架现有的 Python 音频图，不代表与 Chrome 完整音频实现逐样本一致。

DynamicsCompressorNode 使用有状态的单声道压缩内核，包含前瞻延迟、软拐点、补偿增益、检测器及攻击/释放包络，参数在 32 帧边界采样。默认、低振幅、静音、硬拐点及高频三角波有 Chrome 156 数值对照和容差回归；这不代表完整多声道 Web Audio 或逐位一致。
