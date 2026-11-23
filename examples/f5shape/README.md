# F5 Shape / Southwest

```powershell
..\.venv\Scripts\python.exe -X utf8 -B examples/f5shape/xinan.py
..\.venv\Scripts\python.exe -X utf8 -B examples/f5shape/xinan.py --date 2026-12-01 --origin ISP --destination BUR
```

在仓库根目录运行，默认日期为脚本 `DEFAULT_DATE` 指定的 `2026-11-30`、路线 ISP → BUR，代理 `http://127.0.0.1:6696`，等待预算 60 秒。支持 `--proxy none`、`--timeout`，完整参数见 `--help`。

加载实时 booking 页面、SDK 与 bootstrap，从当次响应读取公开 API key 和应用版本。业务请求使用 `page.net.fetch()` 进入页面 SDK 的包装，检查 `Ee30zvqlwf-A/B/C/D/F/Z` 六项请求头，再发送 shopping 请求。

`await run(date=..., origin=..., destination=..., proxy=..., timeout=...)` 返回航班列表。CLI 在发送 shopping 请求前打印六项 Shape 头的完整实际值；之后打印航班数量、航班号、出发时间、路线与成人票价，不打印 Cookie。没有保存结果或增加业务 POST 重试。

成功要求 shopping 返回 HTTP 200 且 `airProducts` 为列表；只取得 Shape 请求头不算查询成功。接口拒绝时显示实际状态与响应摘要，退出码为 1。

历史状态：2026-11-05 曾返回 `403 / {"code":403050700}`。本次简化未重新验证线上航班查询；本地分析报告不随案例分发，也不作为运行输入。
