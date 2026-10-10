# 已分配任务但领取响应丢失：复现与恢复

这些实验使用真实 HTTP、SQLite、生产 Client/Worker，执行器为可信 fixture，`llm_calls=0`。

修复前，在 SQLite 分配事务提交后断开首次 claim HTTP 响应。Worker 没有执行任务；同 worker 再领取得到空结果，因为它已经持有活动租约。受控时钟从 1000 推进至 1031 后，其他 worker 才以 generation 2 领取。`before.json` 保存原始结果及生产源码 SHA；`before-probe.py.txt` 保持原脚本字节。原脚本按最初位置 `.delivery-tools/probe_scheduler_lost_claim.py` 运行，目标为旁边的 `repo-delivery-agent`；复现旧行为需要其记录的旧源码，不能直接把它在新版本上的行为称作 before。

修复后，为一次逻辑领取持久化 request ID，未知传输结果使用同 ID 有限重试；已经收到领取响应后，下一次领取使用新 ID。真实断连回归 3 项全部通过，0 skip，1.660 秒：

1. claim 已提交、回复丢失：两次请求同 ID，恢复同 token/generation，仅执行一次，第一代成功。
2. completion 已提交、连续三次回复丢失：Worker 报 `commit_unknown`，下一次使用新 ID，两个任务分别执行一次；数据库均为第一代成功。
3. completion 请求已读但未提交、三次连接中断：Worker 报 `commit_unknown`；新 ID 不会重新取回仍活动的同一租约，执行器没有重放。

`after.json` 保存安全观测与测试时的源码 SHA，执行前后源码一致。未输出 bearer token、lease token、request ID 或临时目录。运行回归：

```powershell
python -m unittest discover -s tests -p test_scheduler_claim_recovery.py -v
```

这证明了确定性的响应丢失恢复行为。历史 Windows 负载缺少逐请求传输记录，不能据此断言其租约过期一定由这一网络事件引起；[d39 原始失败](../ci-final-closure/validation.json)保留。后续 `00dffae` 已完成[双平台 1000 任务 / 8 进程负载验收与各 409 项测试](../ci-claim-recovery/validation.json)，全部通过，原始报告与完整回读分别归档。
