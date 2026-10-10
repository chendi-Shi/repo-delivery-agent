# 30 秒租约负载通过；普通 Windows CI 有独立 HTTP 错误

提交 `1b7dfd92f568529a85ad67976c78eadc84747773` 将负载工作流租约从 5 秒显式调整为 30 秒，仍使用 1000 个普通任务、8 个独立 worker 进程、300 秒总期限以及原来的严格单次尝试/全部确认条件。主调度实现和验收断言未随本次配置更改。

两系统原始负载报告均通过，并已独立重算逐项回读：普通任务 1000/1000 成功、全部只有一次尝试、1000 条 worker 确认；含故障演练共 1005 条记录，1003 成功、2 取消，缺失和重复接受均为 0。强杀重派、旧租约在新租约仍活跃时被拒绝、取消、协调器重启均通过。[完整收据](validation.json)

| 指标 | Linux | Windows |
| --- | --- | --- |
| 原报告总耗时 | 40.548 秒 | 83.297 秒 |
| 活跃处理窗口吞吐 | 319.754 jobs/s | 49.627 jobs/s |
| claim→commit p50 / p95 | 0.009862 / 0.044979 秒 | 0.035585 / 0.319464 秒 |
| 幂等完成回复 | 0 | 1 |

吞吐分母为首个普通任务领取至最后提交的窗口，排除入队、回读、空闲退出和其他故障阶段；总耗时包括真实等待被强杀 worker 的租约过期。Windows 的幂等回复不是重复接受。每次只有一个主机、业务执行器为 fixture，`llm_calls=0`，不代表多物理机器或模型性能。

**普通 CI 尚未全绿。** Linux 375 项测试、0 跳过，通过，42.175 秒；Windows 375 项、0 跳过、1 error，84.252 秒。`test_authentication_required_for_reads_and_writes` 的错误 token POST 在读取响应状态时收到 `ConnectionAbortedError / WinError 10053`，未取得测试要求的 401。仅凭此日志不能确定根因；它与本次负载通过是独立执行结果。Docker fixture 两项通过；旧模型补丁 verifier 仍为 6 尝试、0 提交、0 成功，不能将 job 运行成功作为模型交付成功。

原 ZIP 均核对 GitHub SHA/大小并保存在忽略目录 `runs/scheduler-load-lease30-1b7dfd9-downloads`。两个平台目录各含原报告、manifest 和 17 个原字节片段；按 manifest 顺序拼接并核对各片及整体 SHA/长度，即可还原 1005 条 JSON 记录。原有 [8dff 的 5 秒失败](../validation.json) 与 [ca963 的 5 秒失败](../additional-5s-ca963a6/validation.json) 保留；30 秒配置通过不能倒推 5 秒配置已稳定。
