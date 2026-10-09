# 官方 SWE-bench smoke 评分

这个流水线只给已经生成的真实预测评分。它不调用推理模型，也不构造
预测、gold patch 或通过结果。三题小样本 smoke 不能代表完整 500 题
SWE-bench Verified 的模型成绩。

## 固定输入与上游

- 数据：`SWE-bench/SWE-bench_Verified`，revision
  `03e151cf5560b1af6a4363c6a9d766deaaea6b56`。
- Parquet SHA256：
  `bb5b123d29ce70107cc0951cf444894241c570a11d76aec452332c65b01e06d8`。
- 官方 harness：
  [`02e7a74ffd0b707aab73d203fe87bdc7c76afc8e`](https://github.com/SWE-bench/SWE-bench/blob/02e7a74ffd0b707aab73d203fe87bdc7c76afc8e/swebench/harness/run_evaluation.py)，
  package version `5.0.2`。安装后核对 PEP 610 `direct_url.json` 的 VCS commit，
  并记录实际脚本、Python、pyarrow 版本。
- 选择：`examples/official-smoke/source.json` 中三个固定 instance ID。
- 预测：`experiments/official-predictions.jsonl`。
- 全部真实尝试：同名追加 `.attempts.json` 的 hash-bound sidecar。
- 从预测和 attempts 读取真实模型标识，并要求同一 run 只包含一种模型。
  使用小模型 smoke 时也如实保留其标识，不标成另一个模型。

官方 v5 的
[`make_test_spec`](https://github.com/SWE-bench/SWE-bench/blob/02e7a74ffd0b707aab73d203fe87bdc7c76afc8e/swebench/harness/utils.py)
要求完整记录中的 `image`、`eval_script`、`log_parser`、`eval_type`、
`FAIL_TO_PASS` 和 `PASS_TO_PASS`。公开 Agent manifest 不含这些评分数据。
CI 在临时目录重新下载固定 Parquet，核对整个文件 hash、500 条记录、
选中任务的镜像名与 eval-script hash，然后才把完整记录交给官方 grader。
它不会用实时 Hugging Face 主分支替换固定快照。

## 工作流

`.github/workflows/official-smoke.yml` 在官方实验文件、脚本或固定输入
变化时触发，也支持 `workflow_dispatch`。没有预测或 sidecar 时保留
`waiting_for_predictions_and_attempts`；真实尝试均失败、没有提交时保留
`no_submissions`。这些状态没有官方分数，也不标为评测完成。

对有补丁的提交逐题运行原始官方 `swebench.harness.run_evaluation` CLI，
`max_workers=1`。每题默认测试超时 900 秒，含拉取镜像的进程上限为
1500 秒，整个 job 上限 90 分钟。镜像只在本 job 新拉取时才尝试删除，
不运行全局 Docker prune。开始下一题前要求至少 8 GiB 空闲空间，运行
期间低于 2 GiB 或私有 stdout 达到 32 MiB 就停止并记录基础设施问题。

每个进程结束后核对并清理该 run ID 的对应题目容器，不清理其他任务。
实际镜像 ID/digest 留在证据中，因为官方记录给的镜像 tag 是 `latest`。
最后使用固定官方
[`make_run_report`](https://github.com/SWE-bench/SWE-bench/blob/02e7a74ffd0b707aab73d203fe87bdc7c76afc8e/swebench/harness/reporting.py)
对全部真实尝试生成官方 aggregate，再由项目核对每题 report 和 submitted
patch 的匹配关系。

该固定 v5 的
[`RUN_EVALUATION_LOG_DIR`](https://github.com/SWE-bench/SWE-bench/blob/02e7a74ffd0b707aab73d203fe87bdc7c76afc8e/swebench/harness/constants/__init__.py)
实际为 `logs/evaluation`。脚本从已校验的官方 constants 读取目录，并核对
布局；每题 report 和最终 `results.json` 均来自 `logs/evaluation/<run_id>`，
随后仅把允许的部分导出到 `runs/official-smoke/reports`。

## 分母和未知结果

- `attempted_denominator`：真实尝试数，包括未提交和失败的尝试。
- `submitted_only_resolved_rate`：已提交集合的成绩，单独列出。
- `all_attempt_resolved_rate`：官方确认解决数 / 全部真实尝试数。
- missing 或 invalid 官方 report 保留未知，整体评分不完整时 rate 为 null。
- 尚未尝试的固定任务列在 `unattempted_ids`，不假装已运行。
- 官方 CLI 会跳过空 patch，并在官方 aggregate 标为 `empty_patch`。只有
  匹配该官方结果时才确认空提交为零贡献；全是空 patch 时状态为
  `empty_submissions`，不会声称测试完成。
- 官方基础设施和 ambiguous failure 分类另列，便于解释失败原因。

## 上传什么

`runs/official-smoke` 只包含明确允许的证据：

- `summary.json` 与安全的 `events.jsonl` 编排日志。
- 官方每题 report 的 boolean 字段；原始 report hash 和导出字段另存。
- 与提交预测一致的 `patch.diff`，用于验证报告确实对应这份模型补丁。
- 官方 aggregate 的数量、任务 ID 和资源清理状态。

**不会上传** Parquet、完整 grading rows、gold/test patch、`eval.sh`、
`test_output.txt`、`tests_status` 和原始 harness stdout。原始运行日志可能
包含评测测试片段，因此只在临时评分目录保留到 job 结束。

```bash
python scripts/official_smoke_grade.py --check-inputs
python -m pip install -e . pyarrow \
  'swebench @ git+https://github.com/SWE-bench/SWE-bench.git@02e7a74ffd0b707aab73d203fe87bdc7c76afc8e'
python scripts/official_smoke_grade.py --output runs/official-smoke
```

本机 Docker 引擎不可连接时不生成评分结论。此流程需要真实 Linux Docker
runner 完成，并从该次 GitHub Actions artifact 读取实际结果。

## 独立官方容器契约 fixture

`.github/workflows/official-harness-contract.yml` 运行
`scripts/official_harness_smoke.py`，无需等待模型提交。它复用上述数据与
harness 固定版本，只取公开任务 `psf__requests-2931`，提交固定的
`harness-contract-fixture` 补丁：仅新增 `delivery_harness_smoke.txt`，内容
明示没有 LLM 调用、不是 issue 的解答。补丁不依赖 gold，不修改应用代码。
私有临时目录中的这条 fixture 预测直接交给原始官方 CLI，不经过真实模型
`export_predictions`，不生成 Submitted 证据或 attempts sidecar。

只有 CLI 正常退出、无超时，且官方报告与固定补丁一致，三个 boolean
严格为 `patch_successfully_applied=true`、`resolved=false`、
`infra_failure=false`，契约才通过。固定官方
[`get_logs_eval` / `get_eval_report`](https://github.com/SWE-bench/SWE-bench/blob/02e7a74ffd0b707aab73d203fe87bdc7c76afc8e/swebench/harness/grading.py)
需要实际可解析测试输出才会把 `patch_successfully_applied` 设为 true；
缺报告、缺字段、补丁不匹配或测试基础设施异常均使 job 失败。

该单任务 job 上限 30 分钟，测试 900 秒、操作 1500 秒，复用原有磁盘、
私有日志和容器清理限制，并只清理本任务新拉取的镜像。只上传 boolean
report、固定 fixture patch 和安全契约 metadata。summary 明确写入
`fixture=true`、`llm_calls=0`、`scope=functional_contract`、
`model_score_eligible=false`；模型评分字段为 null。这个结果验证容器评分
链路，永远不计入真实模型成绩；原真实预测工作流的 Submitted gate 不变。

```bash
python scripts/official_harness_smoke.py --output runs/official-harness-fixture
```
