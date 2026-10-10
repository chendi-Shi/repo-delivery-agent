# 固定队列第一轮：交付结果仍未知

本轮保留全部 10 个预定目标行：8 个目标实际发起模型请求，2 个知识库目标因成功来源不可用而在调用前阻塞。没有目标提交，没有可确认的独立验收结果，10 个目标的交付结果均为未知。不能据此宣称成功交付、知识迁移闭环或效率提升，也不能把它报告为“模型解题成功率 0/10”。

原始运行：[GitHub Actions 38017239837](https://github.com/chendi-Shi/repo-delivery-agent/actions/runs/38017239837)，代码版本 `a662b2bb25d5c0174ef4b49d7802e1ad7d2bde37`。制品 ID 为 `11656764603`，下载 ZIP SHA256 为 `8a01ad661efc08c8cf1adaa122c9fa46ec15ab90ba41dc19c0d899734a9b3635`。来源见 [artifact-provenance.json](artifact-provenance.json)；原始收据和 [cohort-summary.json](cohort-summary.json) 保持不变。

固定模型为 `qwen3:4b-instruct`，权重摘要 `0edcdef34593eac1aa2be9c7d06c432dcf81945adca5eca2f27662c18f168ba0`。每次预算为 24 步、24,000 上下文参数、1,500 最大输出、1,800 秒运行上限、300 秒单请求超时；种子为 42、43。完整任务、镜像、模型及工具标识见 [suite-plan.json](suite-plan.json)。后续改动的超时设置不适用于本轮结果。

| 目标 | 种子 | 策略 | 执行结果 | 已记录请求数 | 运行秒数 | 独立交付结果 |
| --- | ---: | --- | --- | ---: | ---: | --- |
| last default_factory | 42 | baseline，无知识 | 基础设施异常，未提交 | 1 | 248.799 | 未知 |
| last default_factory | 42 | retrieval，无知识 | 基础设施异常，未提交 | 8 | 1,108.582 | 未知 |
| last default_factory | 42 | retrieval，已验证来源 | 来源不可用，调用前阻塞 | 0 | 未记录 | 未知 |
| last default_factory | 43 | baseline，无知识 | 基础设施异常，未提交 | 1 | 250.024 | 未知 |
| last default_factory | 43 | retrieval，无知识 | 基础设施异常，未提交 | 1 | 300.864 | 未知 |
| last default_factory | 43 | retrieval，已验证来源 | 来源不可用，调用前阻塞 | 0 | 未记录 | 未知 |
| split_after keep_separator | 42 | baseline，无知识 | 基础设施异常，未提交 | 1 | 300.973 | 未知 |
| split_after keep_separator | 42 | retrieval，无知识 | 基础设施异常，未提交 | 4 | 496.970 | 未知 |
| split_after keep_separator | 43 | baseline，无知识 | 基础设施异常，未提交 | 4 | 491.810 | 未知 |
| split_after keep_separator | 43 | retrieval，无知识 | 基础设施异常，未提交 | 1 | 300.838 | 未知 |

表中已启动目标的请求数来自哈希绑定的 `summary.observed_provider_requests`，是发起记录，不等于成功响应数。两个阻塞行的零请求依据 `attempt_started=false`、`attempted_count=0`、`model_calls=0`；它们没有独立的请求日志计数字段。8 个已启动目标合计记录 21 次请求。全部已启动目标的完整 Token 用量和原始 `model_calls` 均未导出为已知测量，不能用请求数或上下文估算替代 Token。

独立来源任务 `first default_factory` 不进入目标分母。其收据记录 1 次实际尝试、1 次请求、300.949730128 秒、未提交、交付结果未知；Token 和原始模型调用量仍未知。来源包 `eligible=false`，原因是 `source_not_independently_delivered`，所以两个知识库目标未执行。没有可验证的目标知识读取。

9 份调用前基线收据（来源加 8 个实际目标）均确认既有回归检查通过且新功能缺口存在。这些是零模型调用的环境/任务检查，不是 Agent 成功。安全导出的 9 份实际尝试摘要只给出通用 `ValueError` 基础设施标签，缺少最终模型产物、动作审计和独立验收结果；仅凭这组公开制品，不能把全部异常归因为同一种模型、算法或超时问题。

baseline、retrieval、verified-source 的固定目标分母分别为 4、4、2；已确认成功数均为 0，未知交付数分别为 4、4、2，所以交付成功率均为 `null`。baseline/retrieval 总 Token 为 `null`；两个调用前阻塞行的零 Token 是已知未调用。三类对照共有 4、2、2 个预定配对，成功配对均为 0，效率差异及每次成功交付成本均不可计算。已启动目标记录的秒数合计为 3,498.858557590；另列来源 300.949730128 秒。这是各次运行时间之和，既不是并行 CI 的墙钟时间，也不覆盖模型下载、镜像构建、来源准备或独立验收的完整系统成本。

本地使用加强证据校验后的汇总器重新读取原始文件，输出到新文件 [cohort-summary-revalidated.json](cohort-summary-revalidated.json)。汇总器 SHA256 为 `ad37432512aef1203dd57a07ba69bd6f3001e0229620a09f6d3ac84d38771c11`；计划 SHA256 为 `a7d65a20ce8a8eb1ac341c89d51e78e6766e0bb6a5955ad1fd8b63990f1ed9ad`，观测清单 SHA256 为 `271e35cfcc34b9a97df958d32d1ef71e927250c68b701b26cf9e475e4aa7c6e5`。没有重跑模型或修改原始测量。

在仓库根目录执行同等命令：

```text
python scripts\summarize_closure_suite.py --plan experiments\closure-cohort-v1\suite-plan.json --observations experiments\closure-cohort-v1\observations.json --output experiments\closure-cohort-v1\cohort-summary-revalidated.json
```

退出码为 `1`，表示队列仍有未知结果；解析和证据校验成功完成。标准输出为：

```json
{"expected_target_attempts": 10, "complete": false, "global_improvement_claim": false}
```

后续补充读取了全部 8 个已启动目标的 GitHub Job 日志，已能区分底层异常：**6 个 `TimeoutError`，2 个 `ContextBudgetExceeded`**。历史运行器在输出摘要时将底层异常覆盖为通用 `ValueError`；原始摘要标签保持不变。安全补充文件 [job-log-diagnostics.json](job-log-diagnostics.json) 仅保存任务、Job 链接、异常类型和 UTC 时间，不包含原始日志。

| 目标 / 种子 / 策略 | 日志确认的异常 | GitHub Job |
| --- | --- | --- |
| last / 42 / baseline | ContextBudgetExceeded | [114111488765](https://github.com/chendi-Shi/repo-delivery-agent/actions/runs/38017239837/job/114111488765) |
| last / 43 / baseline | ContextBudgetExceeded | [114111488896](https://github.com/chendi-Shi/repo-delivery-agent/actions/runs/38017239837/job/114111488896) |
| last / 42 / retrieval | TimeoutError | [114111488700](https://github.com/chendi-Shi/repo-delivery-agent/actions/runs/38017239837/job/114111488700) |
| last / 43 / retrieval | TimeoutError | [114111488815](https://github.com/chendi-Shi/repo-delivery-agent/actions/runs/38017239837/job/114111488815) |
| split_after / 42 / baseline | TimeoutError | [114111488730](https://github.com/chendi-Shi/repo-delivery-agent/actions/runs/38017239837/job/114111488730) |
| split_after / 42 / retrieval | TimeoutError | [114111488754](https://github.com/chendi-Shi/repo-delivery-agent/actions/runs/38017239837/job/114111488754) |
| split_after / 43 / baseline | TimeoutError | [114111488726](https://github.com/chendi-Shi/repo-delivery-agent/actions/runs/38017239837/job/114111488726) |
| split_after / 43 / retrieval | TimeoutError | [114111488737](https://github.com/chendi-Shi/repo-delivery-agent/actions/runs/38017239837/job/114111488737) |

来源任务另已确认出现 `TimeoutError`，不计入上述 8 个目标统计。这些诊断补充了失败分类，没有补出缺失的独立验收结果，也不能据此推断内存耗尽、具体执行动作或模型是否完成了正确修改。10 个目标的原始交付结果仍为未知。
