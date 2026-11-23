# PX3 / Skyscanner

```powershell
..\.venv\Scripts\python.exe -X utf8 -B examples/px3/px3.py --url "当前完整挑战页 URL"
```

在仓库根目录运行。默认代理 `http://127.0.0.1:6696`、等待预算 120 秒、按住 2.5 秒；支持 `--proxy none`、`--timeout`、`--hold`。

`--url` 应使用当前挑战地址及当次 `uuid`、`vid`。脚本顶部保留原示例 URL，历史标识可能过期。脚本等待 PRESS 按钮、读取布局坐标、执行原有按住／释放操作，再等待路径离开 `/sttc/px/captcha`。

`await run(url=..., proxy=..., timeout=..., hold=...)` 返回最终 `url`、`title`、`elapsed`。终端打印关键进度、完整的 `/xhr/` POST 荷载、离开挑战页后的完整 Cookie 与结果。二进制荷载以 Python 字节字面量完整显示。已移除 `--trace`、`--log`、子进程启动与反复 `run_until_idle()` 的包装。

退出码 0 表示观察到离开 captcha 路径；仍停在挑战页时超时返回 1。该信号未进一步验证落地页业务内容。本例尚无本次线上通过记录。历史框架分析保留于 [px3.md](px3.md)，其中旧 CLI、分析工具路径和测试数量仅供历史参考。
