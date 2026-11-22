# 网络传输

**简体中文** | [English](../en/pyv8c/network-transports.md)

[文档索引](../README.md)

## 传输角色

HTTP 和 WebSocket 是两个可分别替换的角色。默认 HTTP 为 `curl_cffi`，WebSocket 为 `websockets`；`cronet` 可选，使用 cycronet 依赖。两个内建 HTTP 引擎都基于同一套可取消的异步投递骨架，WebSocket 只有 `websockets` 一种内建实现。

```powershell
python -m pip install -e .
# 需要 cycronet 时单独安装：
python -m pip install -e ".[cronet]"
```

Browser 默认开启真实网络；`real_network=False` 不装配传输，用于本地资源测试。缺失所选引擎依赖会明确失败，不静默切换另一引擎。

```python
from pyv8c.async_api import Browser, run

async def main():
    async with Browser(http_engine='curl_cffi', ws_engine='websockets') as browser:
        page = browser.new_profile().new_page()
run(main())
```

`http_engine`、`ws_engine` 可以是注册名、符合协议的对象、每个 Profile 创建一次的工厂，或 None。None 表示缺少对应角色，不等于禁止所有 loader 和本地资源。

## 参数与代理

`http_options`、`ws_options` 交给对应引擎。内建引擎从自身与底层客户端可接受的键建立校验，选项拼错或传错角色会在创建 Profile 时失败。支持集合取决于当前安装版本，不应把另一引擎的参数直接复制过来。

curl_cffi 常用参数包括 `impersonate`、`verify`、`timeout`，框架还处理 `default_headers` 和统一 `proxy`。`impersonate` 只影响所选传输，不会同步改变 IDL、navigator、设备或页面环境。

Browser 的 `proxy` 是统一的 HTTP/HTTPS 代理入口，Profile 可覆盖；未设置或空字符串表示直连，不自动继承系统代理。支持 URL 中的代理凭据；统一代理模型不支持 SOCKS。不能声明支持代理却静默忽略配置。

## 宿主请求与页面请求

宿主 HTTP、页面资源和 Worker 的每个 HTTP 请求或跳转共用 Profile 调度。根 Page、iframe 和 Worker 共用普通请求额度，宿主请求在每组内最多占两个位置。导航优先调度，同步资源请求最多占一个位置，两者各有有界队列。curl_cffi 在共享连接池中分配额度，Cronet 为导航和同步资源使用独立连接池，并与普通请求共享框架 CookieJar。同一请求的排队、预检和重定向共用截止时间。已排队的请求可取消，页面关闭会等待实际传输完成。

内建引擎直接提交到 I/O 循环。页面异步资源和 Worker 的磁盘缓存交给 loader 线程池，并由 Page 在关闭时等待。同步资源调用等待传输完成，不依赖它正在阻塞的 owner 循环。

curl_cffi 至少有十个连接时为同步资源预留一个位置。两到九个连接时，同步资源与普通请求轮流使用普通额度，导航额度保留。只有一个连接时，导航、同步资源和普通请求轮流使用它。

| 入口 | 行为 |
| --- | --- |
| `await page.net.get(url, ...)`、`await page.net.request(method, url, ...)` | 宿主网络请求，不执行页面 Fetch，也不完整应用页面资源策略 |
| `await page.net.fetch(...)`、`await page.net.xhr(...)` | 调用页面当前 Fetch/XMLHttpRequest，等待宿主响应快照 |

宿主请求常用关键字包括 headers、params、data、json、cookies、timeout、referer 和 options。宿主与页面请求共享 Profile Cookie 状态，但安全策略、拦截器、缓存和页面覆写行为不同。

NetworkRequest 使用关键字构造，包含 URL、method、headers、body、timeout、credentials、mode、redirect、allow_redirects、origin、site_for_cookies、top_level_navigation 和 options。它是冻结的传输输入：header 与 Cookie 在构建期一次性定稿，下游要调整（例如排队阶段的剩余超时预算）须用 `dataclasses.replace` 派生新请求。options 只承载传输扩展键，未知键被传输忽略。NetworkResponse 是普通响应数据，提供 status、headers、body、text、content、json() 等读取入口；它不是 Web Response。

Profile 持有唯一的 CookieJar。第一跳按请求的 credentials 和站点上下文生成 Cookie。两种内建引擎共用逐跳策略：先写入响应的 Set-Cookie，再按下一跳 URL 重新选择合适的 Cookie。同源跳转保留显式 Cookie 头；跨源跳转移除显式 Cookie、Cookie 覆盖值及 Authorization，再按目标站点选择 Cookie。document.cookie 另受页面规则约束。就绪传输实例只能被一个活跃 Profile 拥有；多个 Profile 应使用工厂或注册名。

Cookie Domain 使用依赖内置的 Public Suffix List 快照检查；`SameSite=None` 要求 `Secure`，并校验 `__Secure-` / `__Host-` 的部分属性。页面请求按发起站点、祖先站点和顶层导航方法过滤 Strict、Lax 及默认 SameSite Cookie；宿主请求不自动应用页面站点上下文。分区 Cookie 和浏览器完整 Cookie 策略尚未实现，不能将 CookieJar 当成 Chrome 安全边界。

跨源 CORS 请求在需要时先发送不带凭据的 OPTIONS 预检，并验证来源、凭据、方法和请求头。预检通过后，Window 和 Worker 可以复用同一 Profile 内与顶层来源、发起来源、目标 URL、凭据模式、方法及请求头名称完全匹配的授权。有效期由 `Access-Control-Max-Age` 决定，最长 600 秒。无法逐跳控制重定向的传输会拒绝需要预检的请求。流式请求体、XHR upload 监听触发的强制预检和私网访问策略尚未实现。

## 真实 wire 回归

`tests/integration/wire_parity_harness.py` 与 `test_engine_wire_parity.py` 用真实本地服务器复现同一场景，分别验证两个内建引擎：导航头与调用方自定义头、CookieJar 唯一真相与多值 `Set-Cookie` 写回、gzip 与 204 空体、POST 请求体与 content-type、重定向链只交付最后一跳、跨源跳转剥离 Cookie、以及通过统一 proxy 承载请求。录制按到达顺序保存每一跳的请求头、请求体与多值响应头；服务器端口由 bind 0 分配，`daemon_threads` 配合 `shutdown()`/`join()` 回收，记录属于夹具实例而非模块级全局，因此可用 `--dist loadscope` 并发执行。

Chrome 请求头的绝对值与 libcurl 头顺序基线仍由 `test_request_headers_on_the_wire.py` 和 repros 守护，新回归不复制第二套易漂移常量，也不把两个底层栈的头顺序差异当成缺陷：比较前剔除传输放置的 `Host`、`Connection`、`Content-Length`，以及 Cronet 为抑制客户端自带 jar 而注入的空 `Cookie`；只断言两引擎在框架拥有的头集合与取值上一致。

连接建立阶段的证据来自本地裸 TCP 录制器：它读取一个 TLS ClientHello 后故意不应答，请求必然以握手失败告终，测试断言的是录制到的结构，不需要证书、`verify=False` 或外网。解析只覆盖 TLS 记录头、握手头、cipher suites、supported_versions、ALPN 与 SNI，并把结果规范成无序、剔除 GREASE 的能力集合（TLS 1.3、ALPN `h2`/`http/1.1`、TLS 1.3 cipher 与关键扩展）；这个共享能力集合是回归基线，不锁定 ja3/ja4、套件顺序、曲线或任何绝对指纹值。

跨源夹具使用 `127.0.0.1` 与 `127.0.0.2` 两个 loopback 地址，使 Cookie domain 真正不同；受限沙箱若只暴露 `127.0.0.1`，回退到不同端口并放弃 Cookie 剥离断言，因为 Cookie 不按端口隔离。两种内建 HTTP 引擎都由框架逐跳处理重定向并重新计算 Cookie。cycronet 缺失时只跳过该引擎的参数化用例，不跳过整个模块；模块整体仍要求 native 扩展与 `pytest.mark.native`，离线门禁只运行 unit 测试，不受影响。

## 自定义传输

HTTP 引擎提供 `submit_request(request, handle, remaining)` 和同步方法 `close()`。提交方法可从不同线程调用，应立即返回 `concurrent.futures.Future`。引擎在自己的 I/O 循环启动请求时调用 `remaining()` 取得剩余秒数或 `None`。该请求截止时间到期时，以 `pyv8c.RequestTimeout` 结算 Future。传输自身提前失败时保留原异常，包括引擎自身的 `TimeoutError`。`close()` 在资源释放完毕后返回 `None`，关闭失败时抛出异常。

引擎在开始 I/O 前通过 `handle.attach_abort(callback)` 登记线程安全的中断钩子。取消早于登记时，登记会立即执行钩子。截止时间与外部取消共用一次中断，结果在传输清理完成后交付。可继承 `pyv8c.network.async_delivery.AsyncTransport` 实现异步的 `_single_call()`，复用 I/O 循环、取消、超时和关闭。关闭预算包含默认执行器中的工作，如 DNS 解析。

Page 关闭时停止接收新的 HTTP 请求，主动中断它拥有的宿主和页面资源请求，并等待后台任务与在途请求结束，每次观察到进展时刷新关闭预算。预算耗尽抛出 `HostStateError`，Page 保留未完成的记录，稍后可重试 `close()`。导航替换和资源取消经 `LoadHandle.cancel()` 传给传输；宿主请求的超时和取消走同一份在途记录。

Worker 的取消也会中断正在运行的 HTTP 请求，包括 CORS 预检和重定向的当前一跳。每一跳完成即解绑自己的中断句柄，再处理响应。Page 仍等待传输清理完成。关闭日志在 `background-futures` 中区分 `worker-resource` 和 `host-callback`；已运行的 Python 回调仍须自行及时结束。

两种内建引擎共享 `AsyncTransport`，每次提交只发送一跳。Profile 对导航、Window、Worker 和宿主 HTTP 的每一跳重新做公平准入。资源请求共用预检、重定向和响应策略，宿主请求按自己的重定向选项处理。整个请求共用一个截止时间，排队超时由 Profile 调度器处理。传输在 I/O Task 清理完成后结算 Future。Windows 上 curl_cffi 的专用线程使用 selector loop。

`network/backend.py` 负责引擎所有权、准入和关闭，`network/host_http.py` 负责宿主 HTTP 的重定向、超时和终态交付。宿主与资源请求共用 `network/request_builder.py` 生成传输请求并计算剩余时间。

关闭引擎时先停止接单，再等在途协程完成清理，然后关闭会话与事件循环。超时或失败保留所有权，下次关闭继续清理。

WebSocket 实现 `websocket(tasks, url, protocols, callbacks, options=None)`、`close_websocket(socket)` 和 `close()`。角色协议分别为公开的 HttpTransport 与 WebSocketTransport。

```python
from pyv8c import register_transport

# MyHttpTransport 需实现完整传输协议。
def install_transport(MyHttpTransport):
    register_transport(
        "local-http",
        lambda options=None: MyHttpTransport(options=options),
        option_keys=lambda: frozenset({"timeout"}),
    )
```

注册工厂以 `options=<字典或 None>` 调用；直接传给 Browser 的非注册工厂则无参调用。已有实例及非注册工厂不能再通过 http_options/ws_options 或统一 proxy 配置，应在实例创建时完成设置。`register_ws_transport()` 注册 WebSocket 角色；注册时 `proxy=True` 表示实际支持统一 proxy。异步取消句柄属于传输层，与页面资源 LoadHandle 分工，不能省略迟到结果的所有者验证。

资源层通常要求引擎返回单跳响应，由共享 redirects 规则推进；无法关闭自动跳转的引擎必须明确声明该行为，不能静默吞掉中间跳。

## WebSocket 与限制

内建引擎使用 `websockets.asyncio`，最低依赖 `websockets 17.0`，代理 CONNECT 在取消或超时后会关闭 TCP 连接。一个 Browser 按需启动一个 WebSocket I/O 循环，Profile 各自持有连接和 Cookie。每个 Profile 最多登记 256 条连接，同时最多进行 32 次握手；握手排队计入 `open_timeout`。WebSocket 握手拒绝 HTTP 重定向。

`open`、`message`、`error`、`close` 在所属文档或 Worker 的 networking 任务来源依次派发。消息派发时要求连接仍为 `OPEN`。跨文档导航在派发卸载事件前回收旧连接，切换期间停止接受旧文档的握手。Worker 的停止信号直接中断内建传输的握手。JS 调用 `close()` 时先按 FIFO 发送已接收的数据，再发关闭帧。Page 或 Profile 关闭时取消未完成的握手，给空闲已连接套接字一段关闭握手时间，并等待传输退出。

每条连接的收发额度各为 256 条消息和 16 MiB。接收队列满时暂停读取，直到 Runtime 处理消息。发送额度按排队和正在写入的消息计算，写入结束立即释放额度。发送队列满时连接以异常关闭结束。未抛错的 `send()` 都计入 `bufferedAmount`，已发送的字节由 Runtime 任务合并扣除。库的 `max_size` 和 `max_queue` 仍可配置，默认消息大小为 1 MiB，单消息上限为 16 MiB，底层帧队列保持有界。内建引擎的 `ws_options["create_connection"]` 接收 `websockets.asyncio.client.ClientConnection` 构造器。独立注册的 WebSocket 引擎继续使用 `WebSocketTransport` 接口。

TLS、HTTP 版本、压缩、代理和握手排列受引擎及依赖构建影响；选择某个引擎不意味着与 Chrome 全网络栈一致。具体请求头规则见[请求头](request-headers.md)。

传输协议只交换 Python 请求/响应数据。Browser/Profile/Page 的异步接口保持 V8 线程归属；Profile 等待页面结束后可在线程池中调用 transport.close，以免阻塞宿主循环。传输清理不能进入 V8，也不能依赖宿主循环推进才能结束。

共享网络会话关闭引擎时，每个引擎都会被尝试关闭，任一失败都会上报给宿主而非静默吞掉；失败项保留引用，下一次 `close()` 只重试它们，只有全部成功后才置空引用并标记会话关闭完成。`http_available` 等能力属性在全部关闭前仍描述装配时的引擎，关闭期的准入检查仍是边界，不应据此重新提交请求。
