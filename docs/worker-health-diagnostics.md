# API worker 退出诊断

入口 `backend/run.py` 使用锁定的 Uvicorn 0.54.0。多 worker 的父进程
通过独立心跳线程检查子进程，不是访问 HTTP `/health`。

心跳等待由默认 5 秒调整至 30 秒，为小型服务器短暂争用留出余量。
这是缓解措施，不代表已证明或修复此次 worker 退出的根因；真正无响应的
worker 最多需等待这段检查时间再替换。未修改采集网络超时或完整性要求。

父进程仅在检查失败时记录 `Worker health failure`：
- `alive_before` / `exitcode_before`：发心跳前的进程状态。
- `heartbeat=not_sent`：进程已退出，没有发送心跳。
- `heartbeat=no_response`：检查开始时存活，但没有收到回应；可能超时、
  管道失败或检查期间退出，结合 `alive_after` / `exitcode_after` 判断。
- `elapsed_seconds` / `timeout_seconds`：检查实际用时和等待上限。

这些日志在 supervisor 的 kill 之前产生。随后 `Worker reaped` 记录最终
退出码；单凭最终 `-9` 不能区分外部 SIGKILL 与 supervisor 自己的 SIGKILL。
日志不包含配置内容、凭证、请求正文或进程环境。正常心跳不逐次打印。

诊断只包装 Uvicorn Process 的检查和回收方法，重建、信号处理及启动失败
策略仍由 Uvicorn 原实现负责。入口结束时恢复包装。升级 Uvicorn 时须运行
`tests/test_worker_diagnostics.py`，其中覆盖原 supervisor 的正常/已退出/
无心跳三个分支。需要定位 GIL 或原生代码阻塞时，仍需故障时线程栈和
资源证据；不应从无内核 OOM 日志推断确定原因。
