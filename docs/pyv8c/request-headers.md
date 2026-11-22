# 请求头

**简体中文** | [English](../en/pyv8c/request-headers.md)

[文档索引](../README.md)

## 生成入口

请求头规则集中在 `pyv8c/pyv8c/platform/resources/request_headers.py`，资源请求、宿主 page.net 和 WebSocket 握手通过对应适配器使用。User-Agent、语言及 Client Hints 来自 BrowserEnvironment，应与页面 navigator 的报告保持一致。

请求模式和来源是头生成输入，不仅由目标 URL 决定。发起 origin 使用 Realm 的有效身份，保留继承及 opaque origin 语义。实际线路头仍会被传输引擎和 HTTP 协议影响，不能宣称名称、取值和顺序与所有 Chrome 版本完全一致。

## 场景规则

| 请求 | destination | 典型 mode |
| --- | --- | --- |
| 主文档导航 | document | navigate |
| iframe 导航 | iframe | navigate |
| 经典脚本 | script | no-cors |
| 模块导入 | script | cors |
| 样式、图片、音视频 | style、image、audio、video | no-cors，具体请求配置可调整 |
| Fetch、XMLHttpRequest | empty | cors 或显式请求模式 |

Accept 随资源类型选择；Accept-Language 从语言列表生成。Sec-Fetch-Mode、Sec-Fetch-Dest 记录请求语境，Sec-Fetch-Site 根据发起与目标关系生成。

导航可包含 Upgrade-Insecure-Requests。浏览器或用户动作发起的导航可以带 Sec-Fetch-User，脚本设置 location 或调用元素方法不能直接当作真实用户激活。Referer 按当前引用策略生成，常规默认采用 strict-origin-when-cross-origin；Origin 依方法、模式和跨源关系决定。

Host、Content-Length 及 HTTP 连接信息部分属于传输层。调用方自定义头按适配规则替换或插入，不应在业务实现中重复拼接环境头。

## 排列与协议

框架保留头字典顺序，导航、普通资源、媒体和 WebSocket 使用相应排列。底层引擎可能移动 Host、Connection、Content-Length，HTTP/2 也不等同于 HTTP/1 的文本头排列。

不要用一个传输或 Chrome 版本上的线路记录作为所有环境的固定答案。需要比较时记录客户端版本、TLS/HTTP 协议、页面 origin、方法、body、默认头和用户自定义头，并使用本地回声服务器观察实际到达的请求。

## 实现边界

- site 判断使用 `tldextract` 自带的离线 Public Suffix List 快照，包含私有后缀；其快照可能与具体 Chrome 版本不同。same-origin 仍按精确 origin 判断。
- 完整 CORS OPTIONS 预检尚未覆盖，不能仅因请求头正确就认为 Fetch 安全模型完整。
- 部分 Connection 决策使用 URL scheme 近似，实际协商协议可能不同。
- 媒体请求的 Range、Accept-Encoding 与头顺序存在版本及实现差异。
- WebSocket 握手顺序取决于所选实现；压缩声明与解码能力依赖传输构建。
- 环境 UA、IDL 和 HTTP 引擎标识分别配置，版本不一致时必须单独解释，不能靠覆盖某个头掩盖语义差异。

## 本地验证

```powershell
python -m pytest pyv8c/tests/unit/test_request_headers.py pyv8c/tests/integration/test_request_headers_on_the_wire.py -q
python -m pytest pyv8c/tests/repros/test_request_headers_vs_chrome.py -q
```

请求头和表单对照可以用 `PYV8C_CHROME_BASELINE` 指定 Chrome 154.0.8030.0，以固定请求头行为。未设置时使用 `PYV8C_CHROME` 指定的浏览器或系统 Chrome。对照失败时，分别核查框架、传输层和 Chrome 版本的实际行为，并保留请求头名称、值及顺序的断言。
