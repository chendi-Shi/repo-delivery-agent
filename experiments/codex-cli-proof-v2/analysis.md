# 第二次真实 Codex 尝试：24 步只读操作，未提交

本轮完成了 **24 次真实 CLI 调用和 24 次文件工具动作**，随后以 `LimitsExceeded` 停止。所有动作都属于目录枚举、搜索或读取，没有编辑文件，也没有执行提交动作。保存的补丁为 0 字节，因此仍未形成仓库修复交付。

核对时间为 **2026-10-10 03:51:37 UTC**。独立 Docker 验收尚待执行，当前 `verification_status=deferred`、`delivery_success=null`、`official_resolved=null`。本文保留原始产物和失败口径，不预写后续验收结果。

## 调用、动作与结果

| 项目 | 实际记录 |
| --- | --- |
| 任务 | `psf__requests-2931`；基线 `5f7a3a74aab1625c2bb65f643197ee885e3da576` |
| 实现提交 | `564d8e36fc510896c791f09457cc01775572603d`；运行记录 tracked tree clean |
| 提供方 | `gpt-6-astra` / `codex_cli`，客户端 `codex-cli 0.154.0`，推理强度 `low` |
| 策略与预算 | `recent`，selected-context 字节预算 32,000，reserve 3,048，最多 24 步 |
| 模型调用 | 24 次 CLI 调用尝试，24 次具有完成用量的响应 |
| 实测完成用量 | 输入 **301,010** token，输出 **543** token |
| 整轮耗时 | **357.88484950001293 秒**；不等同于纯模型推理时延 |
| 动作 | `delivery-file/list` **5** 次，`delivery-file/search` **7** 次，`delivery-file/read` **12** 次 |
| 动作返回 | 24 个 `outcome=completed`，全部 `returncode=0`，无动作异常 |
| 文件变化 | `changed_files=[]`，保存的 `model.patch` **0 字节** |
| 停止原因 | `status=stopped`，`exit_status=LimitsExceeded`，`error_type=null` |
| 正式提交 | `submitted=false`，导出提交数 0 |

数据来源为 [summary.json](summary.json)、[evidence.json](model-runs/psf__requests-2931-recent-codex-cli-proof-v2/evidence.json) 和 [完整的公开动作审计](model-runs/psf__requests-2931-recent-codex-cli-proof-v2/action-audit.jsonl)。逐行核对审计得到调用编号 1–24，与 summary 的动作分类计数一致；私有检查点的 `model_usage` 也记录 24 次调用、301,010/543 token、0 次缺失用量。

12 次读取中，11 次读取 `requests/models.py`，1 次读取 `requests/__init__.py`。模型反复枚举根目录，并对 `models.py` 进行了多次重叠范围读取；7 次搜索中有 3 次返回空内容。最后一步仍是 `delivery-file read requests/models.py 1 200`。这些是实际行为观察，不能单凭它们确定究竟是上下文选择、提示、模型策略或其他因素导致没有进入编辑阶段。

工具返回 0 表示这些只读操作执行成功。审计没有 `write`、`append`、`replace` 或提交动作，也没有测试执行；它不能支持“修复成功”或“测试通过”的结论。`files` 后端本来就不执行仓库测试，测试需要独立 Docker 验收。

`patch_valid=true` 仅表示保存的补丁能够在干净基线上应用。本轮是空补丁，这个字段不意味着存在有效修复。`official-export.json` 的 `empty_patch_count=0` 统计已导出提交中的空补丁数；因为提交数为 0，所以该值为 0，不能用于否认本轮实际保存的空补丁。

## 与首轮的关系及后续验证

[首轮 v1](../codex-cli-proof-v1/analysis.md) 在一次成功目录枚举后遇到 `PermissionError`。v2 使用新目录、相同实现代码与相同配置哈希，能够继续到 24 步，未复现该异常；但没有针对首轮根因的代码修复，也没有定位到具体失败调用。这只说明该异常在本轮没有再次出现，不能称为“权限问题已修复”。两轮均为未提交尝试，不能只保留运行得更远的一轮。

下一轮准备采用 `baseline` 策略和 **64,000** 的 selected-context 字节预算，另建 v3 目录观察行为。本分析不把这项准备计为已运行或成功。策略与预算同时改变时，即使后续行为变化，也不能将效果单独归因于其中一个因素；远程模型权重不可固定、provider sampling seed 不受支持，进一步限制了因果比较。

本轮计入 **1 次尝试、0 次提交、1 次未提交失败**；与 v1 合计为 2 次尝试、0 次提交。后续应对各轮保存的补丁分别执行冻结公开检查，保留成功、失败和基础设施未知结果。正式 SWE-bench `resolved` 仍需实际正式提交及匹配补丁的 harness 报告，不能由 CLI 调用成功、补丁可应用或 CI 绿色推断。

## 用量与文件绑定

`usage.complete=true` 的范围是这 24 次外层调用均有 CLI 完成用量；总输入 token 包含多次请求重复携带的上下文和 CLI 原生开销，不能拿 301,010 与单次 32,000 的 selected-byte 预算直接比较。内部网络请求和重试次数不可完全观测。24 个响应的美元费用均未知，`reported_total=null`、`complete=false`；`known_subtotal=0.0` 不表示实际免费。[提供方边界说明](../../docs/codex-provider.md)

下列文件 SHA-256 已从实际文件重新计算。其中 summary、evidence 与私有检查点共同绑定上述用量；私有结果和检查点原文不随本分析公开。

| 文件 | SHA-256 |
| --- | --- |
| [summary.json](summary.json) | `8cda34204753797d271f652c34e9788f5d6429ee9e1753dc627c0d215f44838b` |
| [evidence.json](model-runs/psf__requests-2931-recent-codex-cli-proof-v2/evidence.json) | `2c25ee4151bb24ff79136a2546b6603106bc8876052ba5e8aefb70da0899be9b` |
| [action-audit.jsonl](model-runs/psf__requests-2931-recent-codex-cli-proof-v2/action-audit.jsonl) | `7ac2acbdd245ba8f6a7b055ed56b61438fda9ad41c939ebc21edd306ce3a81d7` |
| 私有 `result.json`，与正式导出来源一致 | `8ff94097b7637e2dc32d29a95279c1c44957d8bf79478826d9ac521b4c17b7d0` |
| 私有 `agent/checkpoint.json` | `40e2f1b071e26e1fa9e91e7ef1b539572b31d0f92e33de992f01b984baf9914d` |

实际文件读取和大小检查确认 [model.patch](model-runs/psf__requests-2931-recent-codex-cli-proof-v2/model.patch) 为 0 字节；证据记录的空内容 SHA-256 为 `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`。v1/v2 的实现哈希均为 `156d950e12d7daadb444f5689fd5a65b5a5a7224e7c3c040896967bb8f5b5277`，配置哈希均为 `75ef615e67e96eaf895616b00f809e7778de1454936c3560ed94ddb7cbc63d53`。两轮保留相同客户端二进制身份，但 `model_digest=null`，不能把客户端哈希解释为冻结的服务端模型权重。
