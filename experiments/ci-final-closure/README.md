# d39b270 原始验证记录

本目录绑定提交 `d39b270c57dc35316ba28dd52c0ac1b9b556294e`。目录名是归档名称，不表示全部验收通过。

- 常规 CI：Linux、Windows 均为 387 项测试通过，0 skip。
- 真实 HTTP + SQLite、8 独立 worker 进程、1000 个 fixture 任务：Linux 通过；Windows 严格验收失败。
- Windows 的 1000 个普通任务最终成功、收到 1000 个确认；但 `load-000118` 第一次租约过期，第二代才成功，因此不满足单次执行要求。没有放宽断言，也没有用最终成功覆盖失败。
- 全量 1005 条记录包含 5 个故障测试任务；这次 workload 的 `llm_calls=0`，不能当作模型交付成功率或多物理机器容量证明。

`validation.json`、`github-metadata.json` 记录运行、任务与原 ZIP 校验值。每个平台的 `report.json` 保持原字节；依照 `report.jobs.manifest.json` 顺序，以二进制方式拼接所有 `.txt` 分片即可还原原始 jobs JSON，并核对大小及 SHA-256。原 ZIP 保留在本地忽略目录 `runs/ci-final-closure-downloads/`，不包含在发布的文本证据中。

历史 CI 没有逐请求传输记录，无法确定此次 Windows 租约过期的具体网络事件。另行进行的“SQLite 已分配但 HTTP 领取响应丢失”实验确定了可复现的恢复缺口；修复后必须使用新提交、新运行验证，不能更改本目录的失败结果。
