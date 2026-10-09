# Repo Delivery Agent

面向真实 Git 仓库的 Python 编程 Agent 交付与评测系统。直接扩展固定版本 **mini-swe-agent 2.4.6**，输入仓库、完整 commit 和需求，输出可应用的 Git patch、完整运行证据与独立验证结果。

原创部分是任务局部历史检索、上下文选择记录、仓库交付流程和评测证据对接。模型适配、原生工具调用和 Agent 基础循环来自上游；没有把它们写成自研框架。

## 能力

| 层 | 实现 |
|---|---|
| Agent runtime | 真实 `DefaultAgent` 扩展、上游 `get_model`、provider 工具协议与成本记录；强制版本检查 |
| 上下文 | baseline / recent / retrieval，保留 system、原始需求和完整工具轮次；超预算在请求前停止 |
| 证据 | SQLite 保存原始消息与 `extra.raw_output`；模型实际看到的请求视图单独保存，不删除原始轨迹 |
| 历史工具 | 通过 bash 调用 `delivery-history`，由 harness 严格解释 search/read/note，不执行宿主 shell |
| 仓库交付 | 独立固定 commit checkout；包含新增文件的 binary Git patch；应用到第二个干净 checkout |
| 执行隔离 | 每 trial 一个持久 Docker 会话，网络关闭、资源限制；源码可写、`.git` 只读；同步回收容器 |
| 并发 | 冻结 batch plan、有界 worker、每 trial 独立模型对象/工作区/容器/证据目录；CPU 默认 1 worker |
| SWE-bench | 去除 gold/hidden test 的冻结任务清单、官方 predictions 导出、固定官方 grader 命令与报告核对 |

**任务已提交、patch 可应用、公开检查通过、SWE-bench 官方 resolved 是四种不同结果。** 没有官方评分的任务不会显示为已解决。

## 安装

Python 3.11+、Git、运行中的 Docker。真实 Agent 运行需要 upstream extras；核心数据验证和上下文测试可仅用标准库。

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
python -m pip install -e '.[agent]'
python -m unittest discover -s tests -v
```

云端模型密钥使用上游 provider 的环境变量；不要写进 task、配置、命令行或仓库。默认不重试模型请求，避免丢失响应后无意重复计费。本地 Ollama 不需要付费 API。

## 在真实仓库执行需求

示例使用 [more-itertools](https://github.com/more-itertools/more-itertools/tree/81c21a8db1c922260cd673841257fb9b112c1318) 的固定 commit，实现一个公开导出的单次遍历 `count_runs` 工具，并覆盖生成器、空输入和不可哈希元素。**这是本项目自建开发任务，不是上游真实 Issue 或 SWE-bench 测试题。** 仓库代码来自真实开源项目，开发验收命令对 Agent 可见。

```bash
docker pull python:3.12-slim
ollama pull qwen3:4b-instruct
python -m delivery_agent run \
  --task examples/more-itertools-count-runs.json \
  --model ollama_chat/qwen3:4b-instruct \
  --api-base http://127.0.0.1:11434 \
  --profile retrieval --output runs/more-itertools-01
```

PowerShell 将命令写成一行，或用反引号续行。本机执行参数、结果与能力边界见 [验证记录](docs/validation.md)。依赖镜像必须事先准备好；任务容器内不能联网安装依赖。自定义仓库应提供适合其语言和依赖的预制镜像。

任务 JSON：

```json
{
  "instance_id": "owner__repo-task",
  "repo": "owner/repo",
  "base_commit": "完整的40字符Git提交SHA",
  "image": "准备好依赖的容器镜像",
  "problem_statement": "需求与可公开的行为契约",
  "public_checks": ["公开的容器内验证命令"]
}
```

每次使用新输出目录。实现不自动恢复中断模型请求或未知命令副作用；失败的 trial 和部分补丁作为证据保留，重试是新 trial。

主要产物：

```text
spec.json                   task/config/upstream/source fingerprint
workspace/                  Agent 编辑的独立 checkout
agent/history.sqlite        完整消息与原始观察
agent/request-views.jsonl   选择/丢弃来源、请求视图、实际 usage
agent/trajectory.json       上游完整轨迹与 cost
model.patch                 可审查的 Git 补丁
validation/                 干净 checkout 应用补丁后的独立工作区
result.json                 提交、patch、公开验证的分别记录
```

## 标准评测

从官方 SWE-bench 数据集导出 JSON/JSONL 到忽略的 `data/`，固定实际来源 revision。原文件可含 gold patch；冻结清单只包含 problem statement、repo、base commit、instance ID 与可选版本。原始数据不能挂进 Agent 容器。

```bash
python -m delivery_agent freeze-swebench --source data/verified.jsonl \
  --dataset princeton-nlp/SWE-bench_Verified --revision DATASET_COMMIT_SHA \
  --output data/frozen-verified
```

冻结记录实际文件 SHA256，并区分用户填写的 revision 与是否远程验证；不能仅凭标签宣称精确复现。`load_manifest` 会核对快照和 task 清单 hash。

标准任务执行与官方评分接口见 [SWE-bench 工作流](docs/swebench.md)。导出只使用真实模型提交的补丁，fixture 永远排除；缺失评分保留未知状态。导出能力不代表跑出了 benchmark 成绩。设计取舍、实验方案和岗位对应见 [设计说明](docs/design.md)。

## 开源依据与取舍

- [mini-swe-agent 固定版本 DefaultAgent](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/src/minisweagent/agents/default.py)：直接继承，复用控制流与模型/环境协议。
- [上游原始工具输出](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/src/minisweagent/models/utils/actions_toolcall.py)：索引 `extra.raw_output`，而不是已经缩短的 prompt 片段。
- [OpenHands condenser](https://github.com/OpenHands/software-agent-sdk/blob/33044182505244749928644a4643368a9e998ee7/openhands-sdk/openhands/sdk/context/condenser/base.py)：参考原始事件与 prompt view 分离、完整工具轮次边界。没有移植其实现，也没有依赖整个 SDK。
- [DeerFlow 任务连续性讨论](https://github.com/bytedance/deer-flow/issues/5378)：参考任务笔记＋历史回查，笔记是 Agent 声明，不是已验证事实。
- [SWE-bench 官方 harness](https://github.com/SWE-bench/SWE-bench/blob/02e7a74ffd0b707aab73d203fe87bdc7c76afc8e/swebench/harness/run_evaluation.py)：导出兼容预测，独立容器评分。上游 batch runner 硬编码 agent class，因此本项目有自己的扩展入口。

## 当前边界

上下文预算使用标记清楚的保守 UTF-8 字节估算，并预留 schema/生成空间，不是模型精确 tokenizer。token 用量以 provider response 为准；未知用量不记为零收益。费用以 trajectory 的 upstream cost 为准，本地模型不能从 token 数推算美元成本。

历史检索是 task-local 的字面相关性排序，未训练学习排序模型；没有多租户服务、分布式调度或跨任务长期记忆。单元/协议/容器验证分别记录，不能替代真实模型成功率或公开基准成绩。公开性能结论需要冻结 task IDs、模型 digest、镜像、配置、源码、失败分母与重复 trial。

MIT License。上游依赖及其许可证仍归各项目所有。
