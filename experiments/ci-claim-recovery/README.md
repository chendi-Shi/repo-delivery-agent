# 领取响应恢复后的双平台验证

本目录绑定提交 `00dffaece1064df3991422e4a5d03aed046cf38c`，原始调度负载运行：[38033408272](https://github.com/chendi-Shi/repo-delivery-agent/actions/runs/38033408272)。每个平台使用一个物理主机、8 个独立 worker 进程、真实 HTTP + SQLite、1000 个普通 fixture 任务及 5 个故障任务，租约 30 秒，`llm_calls=0`。

Linux 与 Windows 的原始报告均通过。重新读取每个平台全部 1005 条记录，严格核对状态、attempt/generation、提交 digest、fixture 结果绑定及单次普通任务执行；1000 个普通任务均第一代成功。原报告各记录 1000 个确认、0 丢单、0 重复接受。强杀 worker 后重派、旧 generation 在新租约仍活动时提交被拒、取消以及协调器强杀重启持久化全部通过。

| 指标 | Linux | Windows |
| --- | ---: | ---: |
| 完整负载及故障实验耗时 | 37.396 秒 | 85.391 秒 |
| 任务处理窗口吞吐量 | 412.09 jobs/s | 42.66 jobs/s |
| claim→commit p50 | 0.0081 秒 | 0.0302 秒 |
| claim→commit p95 | 0.0271 秒 | 0.5208 秒 |

吞吐量分母为第一个普通任务获得租约至最后一个普通任务提交的时间，包含首动作屏障，排除入队、进程启动、最终回读和空闲退出。逐任务时间重新计算后与原报告一致；worker 确认数来自原报告，不能仅从最终数据库记录反推出相同确认数。

`validation.json` 汇总同提交常规 CI 的真实日志测试数量、skip、checkout SHA 和证据重算步骤结果。`github-metadata.json` 保存安全运行及 artifact 元数据；不包含认证材料、临时下载 URL 或原始 CI 日志。

两个 `report.json` 保持下载原字节。按各平台 `report.jobs.manifest.json` 的 `order`，以二进制方式拼接所有 `.txt` 分片，即可还原完整 `report.jobs.json` 并校验原 SHA-256。分片不超过 65,536 字节，保留原换行。原 ZIP 已核对 GitHub digest/大小，并保留在本地忽略目录 `runs/ci-claim-recovery-downloads/`。

[确定性断连回归](../scheduler-claim-recovery/README.md)另行验证 SQLite 已分配但领取响应丢失后的同 ID 恢复，以及提交未知时不重放执行器。[前一次 d39 Windows 失败](../ci-final-closure/README.md)保留；新通过不改变历史失败，也不证明历史网络事件的具体原因。

结果只支持本次有限负载与这些故障场景；不证明多物理机器容量、无限生产规模、恰好一次执行保证或模型交付质量。
