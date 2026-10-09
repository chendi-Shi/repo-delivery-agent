# SWE-bench：真实任务执行与独立评分

这条流程把真实模型生成的仓库改动送给官方 harness。冻结、推理、提交导出与评分是独立步骤；本文提供可执行接口，没有声明任何 SWE-bench 成绩。

运行时固定 **mini-swe-agent 2.4.6**，对应上游源码提交 `04d809ceab9df28f9adaed044884180159172930`。官方评分固定 **SWE-bench/SWE-bench `02e7a74ffd0b707aab73d203fe87bdc7c76afc8e`**。下面的 shell 示例面向 Linux x86_64、Python 3.11+ 和运行中的 Docker，在本项目根目录执行。Windows 可以运行数据准备与标准库测试；官方容器评分建议在 Linux 进行。PowerShell 可把项目 CLI 命令写成一行。

## 1. 准备固定数据与运行环境

将实际下载的完整官方数据快照保存为 `data/private/verified.jsonl`，并从下载记录设置 `DATASET_REVISION` 为该快照来源的真实 40 位 revision。设置 `INSTANCE_ID` 为文件中存在的任务 ID。这里不提供假的 revision 或未核对的样例 ID。

项目已冻结并核验三个官方 smoke 任务，来源完整 revision、原始 Parquet hash 和下载收据见 [官方 smoke](official-smoke.md)。使用这组任务时公开输入为 `examples/official-smoke/manifest.json`，评分输入为 ignored 的 `data/official-smoke/grading-source.json`。

```bash
: "${DATASET_REVISION:?Set the actual 40-character dataset revision first}"
: "${INSTANCE_ID:?Set an instance ID present in the local snapshot}"
python -m pip install -e ".[agent]"
docker info
ollama pull qwen3:4b-instruct
```

源文件可为 JSON 数组、JSONL，或以 `instance_id` 为键的 JSON map。**官方评分在此固定版本要求完整 grader schema**：`instance_id`、`repo`、`version`、`image`、`eval_script`、`log_parser`、`eval_type`、`FAIL_TO_PASS`、`PASS_TO_PASS` 等官方字段。只有 issue/patch 的旧版数据可用于冻结和推理，但不能直接交给此版本评分；应使用与固定 harness 匹配的官方数据。官方 loader 接受 JSON 数组和 JSONL，键值 map 应先在私有数据目录转成数组。

原文件中的 gold patch、test patch、hints 和测试答案留在私有数据文件，只提供给评分阶段。Agent 不加载、不挂载该文件。项目的 `data/` 与 `runs/` 是本地运行产物目录。

## 2. 冻结公开任务

```bash
python -m delivery_agent freeze-swebench --source data/private/verified.jsonl --dataset SWE-bench/SWE-bench_Verified --revision "$DATASET_REVISION" --split test --output data/frozen-verified
```

输出：

| 文件 | 内容 |
|---|---|
| `data/frozen-verified/manifest.json` | 仅 `instance_id`、`repo`、完整 `base_commit`、`problem_statement` 和可选 `version` |
| `data/frozen-verified/snapshot.json` | 原文件 SHA256、公开 manifest SHA256、任务 ID、用户声明的 dataset revision、固定 harness revision |

冻结函数不会远程核验 revision，元数据明确记录 `revision_verified_remotely: false`。可验证的是实际输入字节与公开清单的 hash。执行前会核对清单 hash，修改任务后必须重新冻结到新目录。

## 3. 运行真实模型

示例配置是 [`examples/ollama-swebench-config.json`](../examples/ollama-swebench-config.json)。模型服务运行在宿主机，容器中的任务代码不能联网。配置采用 `litellm` provider；密钥应通过 provider 环境变量提供，配置中禁止 `api_key`、Authorization、凭据 headers 等字段。

也支持直接 Ollama：另存一份配置，将 `model.model_class` 改为 `ollama_native`，在 `model.model_kwargs` 中设置 `mode: "json"` 或 `mode: "tools"`，并按需设置独立服务的 `api_base`。这个入口同样调用真实 DefaultAgent，默认提供内置 Skills；它仍需真实 Docker 任务镜像，files backend 属于普通 `run` 入口。

```bash
export MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT=1
python -m delivery_agent swebench-run --manifest data/frozen-verified/manifest.json --config examples/ollama-swebench-config.json --instance-id "$INSTANCE_ID" --profile retrieval --context-tokens 16384 --reserve-tokens 3048 --output "runs/swebench/${INSTANCE_ID}/retrieval-01"
```

每次 trial 使用新目录。失败命令返回非零并保留 `result.json`，仍应保留在该轮实验的分母中。`baseline`、`recent`、`retrieval` 三个 profile 应使用相同任务、模型与预算，各自保存一套结果。

执行器直接使用上游 `get_sb_environment` 与 `get_model`。根据公开任务 ID 选择上游 SWE-bench x86_64 镜像，在 `/testbed` 执行，冻结容器实际 image ID；初始 HEAD 必须等于 `base_commit` 且工作区干净。结束后再次检查 HEAD，用 `git add -N .` 和 binary Git diff 捕获实际改动；不把 Agent 输出的“已修复”或其提交文本当成 patch。容器同步删除，清理失败的 trial 标记为失败。

容器镜像必须能提供完整依赖；首次运行可能需要从镜像仓库拉取。输入中的自定义 image 字段不会传给 Agent：当前适配器使用上游基于任务 ID 的默认镜像映射，并拒绝 image/workdir/挂载覆盖。它会记录实际镜像 SHA，官方评分则使用私有数据中的 `image`。

产物包括 `task.json`、`spec.json`、`agent/history.sqlite`、`agent/request-views.jsonl`、`agent/trajectory.json`、`model.patch`、`outcome.json` 和 `result.json`。配置只冻结可公开参数及模板 hash，不写入原始凭据。模型调用开始前失败时，可能没有 `agent/` 或 patch 文件。

本地配置的 `cost_tracking: ignore_errors` 对应上游允许值；另一个允许值是 `default`。`ignore` 不是合法值。缺失费用信息不能解释为免费或成本改善。直接 Ollama adapter 会查询并冻结模型 digest；LiteLLM 的 SWE-bench 入口仍需操作者另外保存并固定模型权重，不能只依靠可变模型 tag。

## 4. 导出提交与完整尝试分母

```bash
python -m delivery_agent export --results "runs/swebench/${INSTANCE_ID}/retrieval-01/result.json" --output runs/predictions/retrieval.jsonl
```

一组多任务实验应在 `--results` 后列出**该组所有** `result.json`，包括失败和停止的 trial。不要只挑成功提交文件。重复任务的不同 trial 单独导出，不会自动挑选最好补丁。

仅 `provider: litellm` 或 `ollama_native`、`fixture: false`、`status: submitted`、`exit_status: Submitted` 的真实结果进入官方 JSONL，字段为 `model_name_or_path`、`instance_id`、`model_patch`。空 patch 仍如实保留；它代表空提交，不能解释为解决问题。局部 patch 检查失败也不会被静默剔除。

同时生成 `retrieval.jsonl.attempts.json`，记录全部提供的真实模型尝试、失败状态与源结果 hash，绑定 predictions 文件 hash。fixture 不进入分母。这个 sidecar 只能记录明确提供的结果文件，不能推断未执行或未提供的计划任务。

## 5. 使用固定官方 harness 评分

为官方评分单独创建环境并安装固定源码；以下路径同样从项目根目录使用。

```bash
python -m venv .grader-venv
.grader-venv/bin/python -m pip install "swebench @ git+https://github.com/SWE-bench/SWE-bench.git@02e7a74ffd0b707aab73d203fe87bdc7c76afc8e"
python -m delivery_agent grade-command --predictions runs/predictions/retrieval.jsonl --dataset data/private/verified.jsonl --run-id retrieval-01 --max-workers 1
```

`grade-command` 只打印官方 argv，**不会执行评分**。确认路径后，在 grader 环境执行对应命令：

```bash
.grader-venv/bin/python -m swebench.harness.run_evaluation --dataset_name data/private/verified.jsonl --split test --predictions_path runs/predictions/retrieval.jsonl --run_id retrieval-01 --max_workers 1 --timeout 1800
```

这里用原始私有快照路径，避免评分时重新读取可变的远程 dataset 名称；不能把公开 `manifest.json` 交给官方 grader，因为它已去掉测试答案。当前官方模块的参数使用下划线形式 `--dataset_name`、`--predictions_path`、`--run_id`、`--max_workers`、`--timeout`；本项目 CLI 的连字符参数属于另一层。

使用新的 `run_id` 防止官方 harness 复用旧报告。在当前固定源码中，空 patch 的任务会跳过容器执行，因此通常没有实例报告；聚合时保持未知状态。

## 6. 核对官方报告

```bash
python -m delivery_agent official-report --predictions runs/predictions/retrieval.jsonl --reports logs/evaluation/retrieval-01 --output runs/retrieval-official-summary.json
```

`--reports` 指向单个 run 的目录。聚合器读取官方布局 `<model_name_or_path 将 / 替换为 __>/<instance_id>/report.json`，核对同目录 `patch.diff` 与提交。CRLF/LF 差异可比较，原提交与日志的精确 SHA256 各自保留。报告缺失、布尔字段不合法或 patch 不匹配均保留为缺失/无效，不能记成已解决。

| 结果 | 含义 |
|---|---|
| `status: submitted` | Agent 按协议提交了实际文件改动 |
| `patch_valid` | 独立检查 patch 的结果；SWE-bench 推理阶段为 `null`，官方聚合使用 `patch_successfully_applied` |
| `public_checks` | 开发检查记录；普通 `run` 可指定公开命令，SWE-bench 入口不注入隐藏测试且此列表为空 |
| `official_resolved` | 仅来自匹配的官方实例报告，推理阶段始终为 `null` |

Agent 自行运行的可见测试存在于轨迹，是开发反馈，不能替代官方 `resolved`。

`submitted_only_resolved_rate` 使用已提交 predictions 为分母，并明确标注它只评价提交集合。`all_attempt_resolved_rate` 使用 sidecar 中所有真实尝试为分母，包括 Agent 失败。例如一题提交且官方解决、另外九题失败，分别为 `1/1` 与 `1/10`。这是分母解释示例，不是本项目实测成绩。

只要还有已提交任务缺失或无效评分，两种比率保持 `null`；检查 `missing_ids`、`invalid_ids`、`evaluation_complete_for_submitted`。没有 sidecar 时，整体尝试分母和整体比率保持未知。保留失败文件、数据 hash、模型权重、镜像 SHA、配置、源码版本与所有重复 trial，才有条件比较上下文策略。

对应固定源码：[mini-SWE-agent 环境工厂](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/src/minisweagent/run/benchmarks/swebench.py)、[模型配置](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/src/minisweagent/models/litellm_model.py)、[官方 CLI](https://github.com/SWE-bench/SWE-bench/blob/02e7a74ffd0b707aab73d203fe87bdc7c76afc8e/swebench/harness/run_evaluation.py)、[官方数据 schema](https://github.com/SWE-bench/SWE-bench/blob/02e7a74ffd0b707aab73d203fe87bdc7c76afc8e/swebench/harness/utils.py)。
