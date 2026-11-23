# Reese84 / Nevada 注册代理查询

```powershell
..\.venv\Scripts\python.exe -X utf8 -B examples/reese84/reese84.py
..\.venv\Scripts\python.exe -X utf8 -B examples/reese84/reese84.py --keyword NORTHWEST
```

在仓库根目录运行，默认关键词 `NORTHWEST`、代理 `http://127.0.0.1:6696`、等待预算 60 秒。支持 `--proxy none`、`--timeout`。

打开门户页面并执行原案例指定的 SDK，等待当前 Profile 出现非空 Reese84 Cookie，再 POST 注册代理查询接口。成功要求 HTTP 200 且响应具有 `items` 列表；空列表是有效的查询结果。

`await run(keyword=..., proxy=..., timeout=...)` 返回 `data`、`elapsed`。CLI 在查询前仅打印 `reese84=完整值`，不输出其他 Cookie，随后打印业务 code、总数与前 5 个代理名称。请求仍使用当前 Profile 的完整 Cookie。失败返回 1，日志仅输出到终端。

SDK 路径沿用原案例的 `SDK_PATH` 常量，站点入口变更时需核对。本次简化未重新验证线上业务返回。
