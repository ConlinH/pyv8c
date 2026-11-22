# 资源加载

**简体中文** | [English](../en/pyv8c/resource-loading.md)

[文档索引](../README.md)

## 请求管线

页面导航、元素资源、Fetch、XMLHttpRequest 和 Worker 源加载通过资源能力入口协作。宿主 `page.net.get()` 等直接传输请求不属于相同管线。

常规流程为：规范化请求及冻结发起上下文 → interceptor → 缓存 → 本地协议 → 自定义 loader 或 HTTP → 逐跳响应策略 → 缓存写入 → 结果交付。

本地来源包括 about:blank、data、blob，以及环境声明允许的扩展资源，优先于 loader。interceptor 可直接提供替代响应；替代响应不是完整网络事务，不应假定它执行后续来源的所有缓存与响应策略。

同步和异步请求共用来源及跳转策略，但分别驱动；同步入口不等待自身异步队列。异步最终结果回到所属 owner 队列交付，取消或旧所有者的结果不再修改页面。

## 自定义加载器

加载器签名为 `loader(url, request)`，可返回 Web Response、响应字典或可转换的内容。返回空值不会自动回退真实网络。
不支持的 URL scheme 不会交给加载器。`chrome-extension:` 只处理环境档案中声明且匹配的资源；当前项目不包含扩展文件，返回的是占位内容并记录警告，不能将其视为真实扩展资源。

```python
from pyv8c.async_api import Browser, run

async def main():

    def load(url, request):
        if url == 'https://example.test/':
            return {'body': "<script src='/app.js'></script>", 'headers': {'content-type': 'text/html; charset=utf-8'}}
        if url == 'https://example.test/app.js':
            return {'body': 'globalThis.ready = true', 'headers': {'content-type': 'text/javascript'}}
        raise RuntimeError(f'未提供本地资源：{url}')
    async with Browser(real_network=False, resource_loader=load) as browser:
        page = browser.new_profile().new_page()
        await page.goto('https://example.test/')
        await page.wait_for('ready === true', timeout=1)
run(main())
```

响应字典常用键为 `body`、`status`、`statusText`、`url` 和 `headers`。二进制放入字典的 `body`，不要先转换为文本表示。不同资源应返回对应 Content-Type；未知 fixture 请求明确失败，不能统一伪装为成功空页面。

request 包含 method、headers、body、mode、credentials、destination、resource_type 等请求信息。此字典是回调输入，不是修改任意字段就能重写请求的协议。

## 拦截器

拦截器签名为 `interceptor(url, request)`，每次资源请求在选取来源前调用。

| 返回值 | 行为 |
| --- | --- |
| None 或 True | 继续原请求 |
| False | 返回状态 200 的空响应，不是网络失败 |
| 响应对象、字典或内容 | 作为替代响应交付 |

```python
def intercept(url, request):
    if request.get("resource_type") in {"image", "style"}:
        return False
    return None
```

拦截图片、样式或脚本会改变页面执行和布局，不适合作为完整页面能力验收的默认条件。

## 线程与生命周期

`threaded_loaders=False` 时，普通宿主回调可在页面 owner 上运行，阻塞会阻止该线程推进。`threaded_loaders=True` 使用 Browser 有界线程池；Worker 源加载、Fetch 与 importScripts 的宿主回调强制在线程池执行。池回调只处理输入和普通数据，不访问页面、Realm 或活 JS 句柄，不通过 `page.evaluate()` 反向等待 owner。

LoadHandle 管理请求的原子终态和传输迟绑定。ResourceOwnership 将请求登记到 ResourceScope，并在终态释放登记。取消后，已运行的 Python 工作可以完成清理，后续跳转和页面结果交付停止。

文档资源协调器使用类型化条目、token、导航代次和子文档身份维护加载屏障。iframe 传输完成后仍需等待有效子文档完成；移除、重插、导航或 srcdoc 替换后的旧回调不能释放新屏障或发送旧 load 事件。

Worker 的 ResourceRequestSession 冻结资源配置及有效 origin。importScripts 阻塞 Worker 当前调用，等待普通传输结果，不等待父页面 owner，也不嵌套驱动事件循环。等待沿用当前操作剩余期限；终止或关闭唤醒等待，迟到结果不再执行脚本。

Window、导航和 Worker 使用同一个 `ResourceRequestSession`。它根据发起上下文推进缓存、预检和重定向，交付普通 Python 响应数据。`fetcher` 与 `worker` 分别在 Runtime 或 Worker 所属线程构造 Web Response。启用自定义 loader 或 interceptor 时，仍按配置在线程池或 Window 所属线程运行 Python 回调。

`local.py` 负责协议校验和本地资源解析，`response.py` 负责响应转换。`fetcher.py` 和 `worker.py` 各自管理等待方式与结果回投，复用同一个 Web Response 构造函数。请求句柄在继续处理下一步前解绑已完成的传输。

## 来源、跳转与安全策略

HTTP 与 loader 响应使用共享逐跳策略。301/302 将 POST 改为 GET 并移除 body；303 将 HEAD 以外的方法改为 GET；307/308 保留方法和 body；最多跟随 20 跳。manual/error 模式分别使用相应的重定向响应或失败语义。

同源及 CORS 判断使用请求发起时真实 Realm origin，而非重新从 URL 猜测。导航使用 navigate 模式；普通 fetch、经典元素资源和模块脚本保留各自模式。跨域 no-cors fetch 可产生 opaque 响应；完整预检和 Fetch 标准仍未覆盖。

Profile 内共享 Blob 注册不授予跨源读取权限。先撤销 Blob URL 再新建 Worker 会异步加载失败；构造 Worker 后撤销 URL 不应取消已捕获的源。Blob 字节与 Worker 启动线程之间通过普通数据交接。

扩展资源目前按环境 `extensions` 中的公开规则本地回答占位内容，并不执行真实扩展包或携带完整文件字节。

## 响应缓存

只有 `cache=True` 且指定 `cache_dir` 才启用响应缓存。每个 Profile 先查自己的内存缓存，最多保留 256 条或约 16 MiB。磁盘读写在线程池执行。多个实例指定同一目录时可以共享磁盘文件。

取消与缓存发布权在请求句柄上按先后顺序判定。取消先发生时，写入任务清理临时文件。写入任务先取得发布权时，在工作线程完成原子替换。关闭会异步等待这项工作清理完毕。

GET/HEAD 在满足缓存条件时可写入。键包含方法、URL、模式、凭据、发起 origin 和规范化请求头；携带 Authorization、Cookie 或匹配 CookieJar 内容的请求绕过缓存。请求 no-store/reload/no-cache，及响应 no-store/no-cache/private、Set-Cookie、Vary: *、非成功状态等均不按普通可缓存响应处理。

文件采用普通载荷序列化和原子写入，不保存 DOM、Page 或 Realm 绑定 Response；缓存读写失败不应改变原请求成功结果。当前不提供完整 freshness、ETag/304 或重验证，重定向缓存也不等于 Chrome 的逐跳缓存。对需要严格缓存语义的验证使用 `cache=False`。
