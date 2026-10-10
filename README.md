# Repo Delivery Agent

面向真实 Git 仓库的 Python 编程 Agent 交付与评测系统。输入固定 commit 和需求，执行仓库修改，导出 Git patch，并保留可回查的上下文、恢复状态和独立验证证据。

基础 Agent 循环直接扩展 **mini-swe-agent 2.4.6**。本项目实现上下文与历史管理、Skills、跨任务知识库、检查点恢复、HTTP coordinator/worker、受控执行环境，以及真实仓库和 SWE-bench 的交付评测流程。

详细说明见 [做了什么、怎么做、问题与修复](docs/implementation-walkthrough.md)。真实模型、语义检索和评分证据见 [experiments](experiments)，各阶段验证见 [验证记录](docs/validation.md)。实现功能、协议测试、真实模型提交、独立检查和官方 resolved 分别记录。

固定源任务、十项目标对照、真实读取审计和结果口径见 [真实交付与知识复用实验](docs/delivery-closure.md)。

## 已实现的能力

| 能力 | 具体实现 |
|---|---|
| Agent 与模型 | 固定真实 DefaultAgent；上游 LiteLLM；直接 Ollama HTTP adapter，JSON command/native tools 模式，模型 digest 与实际 usage |
| 上下文工程 | baseline/recent/retrieval；完整工具轮次不可拆；必要消息保护；请求前硬预算；实际 request view 留痕 |
| 任务内历史 | SQLite 保存原始消息与 raw output；delivery-history search/read/note；笔记明确未验证 |
| Skills | SKILL.md 元数据校验、资源 hash、概要→正文→资源渐进披露；两个内置 Python 研发 Skills |
| 跨任务知识 | SQLite WAL、FTS5/BM25、可选真实 BGE embeddings、cosine/RRF；namespace、当前任务排除、来源/验证/过期状态 |
| 恢复 | 原子 checkpoint、OS writer lock、pending→result→commit；已知结果回放、预算连续、未知操作默认停止 |
| 分布式调度 | HTTP coordinator + 独立 worker；持久队列、claim、续租、generation/token fencing、幂等提交、取消和失败重排；可跨机器部署 |
| 仓库交付 | 固定 commit 独立 checkout；新增/修改/删除文件的 binary patch；第二个干净 checkout 重应用 |
| 执行环境 | 持久 Docker、只读 Git 元数据、关闭网络、资源限制和同步清理；或受限源码 files backend |
| 评测 | 公开官方任务冻结、真实 attempts 分母、predictions 导出、固定官方 harness、patch/report 绑定与未知状态 |

**submitted、patch_valid、public_checks、official_resolved 是四种状态。** files backend 只能修改源码，检查保持 deferred；提交成功或 CLI 返回 0 都不能证明测试通过。

## 安装

Python 3.11+ 和 Git。Docker backend 与独立验证需要运行中的 Linux Docker 和预制镜像；Ollama adapter 需要指定服务中的模型。

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
python -m pip install -e '.[agent]'
python -m unittest discover -s tests -v
```

标准库核心不依赖外部服务；真正运行 DefaultAgent 需要固定 upstream extras。云模型密钥使用 provider 环境变量。模型 endpoint 与密钥由 worker 本地配置，不写进远程 task payload。

## 运行真实仓库需求

以下例子使用 [more-itertools 固定版本](https://github.com/more-itertools/more-itertools/tree/81c21a8db1c922260cd673841257fb9b112c1318)，实现单次遍历的 count_runs。这是本项目自建开发任务，包含对 Agent 可见的公开检查。三个官方真实 Issue 另见 [official-smoke](docs/official-smoke.md)。

```bash
ollama pull qwen3:4b-instruct
docker pull python:3.12-slim
python -m delivery_agent run \
  --task examples/more-itertools-count-runs.json \
  --provider ollama_native --ollama-mode json --backend docker \
  --model ollama_chat/qwen3:4b-instruct --api-base http://127.0.0.1:11434 \
  --profile retrieval --output runs/more-itertools-01
```

PowerShell 可写成一行，或使用反引号续行。每个新 trial 使用新输出目录。容器内无网络，运行依赖必须已经存在于镜像。

本机 Docker 不可用时，可先进行受限源码推理：

```bash
python -m delivery_agent run \
  --task examples/official-smoke/psf__requests-2931.json \
  --provider ollama_native --backend files --ollama-mode json \
  --model ollama_chat/qwen3:4b-instruct --profile retrieval \
  --output runs/requests-source-01
```

files backend 只允许 delivery-file list/read/search/write/append/replace，由 Python 解析受限文件操作，不运行模型生成的 shell/Python。read 返回原始源码；write 只创建新文件，已有文件通过精确 replace 或 append 修改，防止用读取片段覆盖整个模块。测试由之后独立的 Docker verifier 或官方 grader 执行。

云模型选择 --provider litellm 和 provider-prefixed model ID。--ollama-mode tools 使用原生函数调用；json 使用严格 command schema，适合缺少原生工具训练的本地模型。

任务格式：

```json
{
  "instance_id": "owner__repo-task",
  "repo": "owner/repo",
  "base_commit": "完整的40字符Git提交SHA",
  "image": "准备好依赖的容器镜像",
  "problem_statement": "公开需求与行为契约",
  "public_checks": ["容器内公开验证命令"]
}
```

run/batch 支持 --max-steps、--wall-seconds、--context-tokens、--max-output-tokens、--request-timeout、--command-timeout、--cost-limit、--seed。上下文预算必须大于生成及协议预留空间。

## Skills 与知识库

默认加载两个内置 Skills 的概要。额外 skill roots 和持久知识库由操作者提供：

```bash
python -m delivery_agent run \
  --task examples/more-itertools-count-runs.json \
  --provider ollama_native --model ollama_chat/qwen3:4b-instruct \
  --api-base http://127.0.0.1:11434 --profile retrieval \
  --skills ./my-skills --knowledge-db data/knowledge.sqlite \
  --knowledge-namespace python-project --embedding-model bge-m3 \
  --embedding-api-base http://127.0.0.1:11434 \
  --output runs/with-knowledge-01
```

嵌入模型可选，默认词项检索不调用服务。--embedding-api-base 指定独立的 Ollama 嵌入服务，默认 http://127.0.0.1:11434；它与生成模型的 --api-base 分别配置。Skills 资源不会自动执行；知识工具只开放 search/read，当前 task ID 从检索和直接读取排除。

delivery-skill list/read、delivery-knowledge search/read 与历史工具均通过 bash tool 调用，由 harness 在 shell 前严格拦截。验证过的 run 自动导入知识库；缺少独立公开检查的记录保留 unverified，默认不进入搜索。详见 [Skills 与知识库](docs/skills-knowledge.md)。

## 批量与分布式执行

本机并发的 --task 指向 RepoTask 对象数组；下面的 examples/tasks.json 需自行准备。

```bash
python -m delivery_agent batch --task examples/tasks.json \
  --provider ollama_native --model ollama_chat/qwen3:4b-instruct \
  --workers 2 --trials 2 --output runs/batch-01
```

Coordinator 与 worker 可跨机器部署，不需要共享文件系统。环境提供同一个至少 32 字符的 DELIVERY_SCHEDULER_TOKEN；默认 loopback，跨机器使用受控网络或 TLS。

```bash
python -m delivery_agent scheduler-server \
  --db runs/coordinator.sqlite --host 127.0.0.1 --port 8765
python -m delivery_agent scheduler-worker --url http://127.0.0.1:8765 \
  --submit task-payload.json --job-id task-01 --max-attempts 3
python -m delivery_agent scheduler-worker --url http://127.0.0.1:8765 \
  --worker-id worker-a --work-root runs/worker-a --api-base http://127.0.0.1:11434
python -m delivery_agent scheduler-worker --url http://127.0.0.1:8765 --status task-01
python -m delivery_agent scheduler-worker --url http://127.0.0.1:8765 --list
python -m delivery_agent scheduler-worker --url http://127.0.0.1:8765 --cancel task-01
```

task-payload.json 包含 task 和 config；格式、TLS、worker-local Skills/KB 和提交语义见 [调度说明](docs/distributed.md)。generation 和 lease token 防止旧 worker 提交。崩溃后任务可能重执行，系统不承诺模型调用 exactly-once。实际验证包括本机独立进程 HTTP 集成，不能写成多台物理机器压力测试。

## 断点恢复

在相同 task、预算、模型、代码、Skills 和 runtime 身份下，对原目录重复相同命令并加入 --resume。已确认的响应/工具结果回放，不重新调用；step/cost/wall-time 预算连续。

```bash
python -m delivery_agent run \
  --task examples/official-smoke/psf__requests-2931.json \
  --provider ollama_native --backend files --ollama-mode json \
  --model ollama_chat/qwen3:4b-instruct --profile retrieval \
  --output runs/requests-source-01 --resume
```

pending 操作的结果未知时，默认停止。显式 --recovery-policy retry 记录决定并重试，可能重复费用或副作用；也可创建新 trial。Docker 恢复要求原容器仍在且挂载/网络/资源约束一致。可控暂停与接管 API 见 [恢复说明](docs/recovery.md)。

## 官方评测与实验

数据来源、三题公开输入、OCI 收据与 v5 schema 见 [官方 smoke](docs/official-smoke.md)。gold 和隐藏评分字段留在 ignored data；模型只接收公开 task。

```bash
python -m delivery_agent freeze-swebench --source data/verified.jsonl \
  --dataset SWE-bench/SWE-bench_Verified --revision DATASET_COMMIT_SHA \
  --output data/frozen-verified
python -m delivery_agent swebench-run \
  --manifest examples/official-smoke/manifest.json \
  --instance-id psf__requests-2931 --config examples/ollama-swebench-config.json \
  --profile retrieval --output runs/swe-requests-01
python -m delivery_agent export --results runs/swe-requests-01/result.json \
  --output runs/predictions.jsonl
python -m delivery_agent grade-command --predictions runs/predictions.jsonl \
  --dataset data/official-smoke/grading-source.json --run-id unique-eval-01
python -m delivery_agent official-report --predictions runs/predictions.jsonl \
  --reports logs/evaluation/unique-eval-01 --output runs/official-report.json
```

grade-command 输出固定官方 harness 命令，不自动执行。独立评分环境安装固定 harness commit 后运行它；不同补丁使用不同 run ID。官方解题结果仅来自 matching patch 的真实报告。

探索上下文策略时固定任务、模型 digest、镜像、seed 和预算，保留所有尝试：

```bash
python scripts/run_experiments.py --task examples/more-itertools-count-runs.json \
  --model ollama_chat/qwen2.5-coder:3b --label local-01 --max-steps 24 \
  --output runs/live-ablation-01 --evidence experiments/local-ablation-01
python scripts/grade_experiments.py --manifest experiments/local-ablation-01/repository-ablation.json \
  --output runs/repository-verification.json --pull-images
python scripts/semantic_smoke.py
```

每次复跑使用新的 output、evidence 目录和 label；脚本拒绝覆盖已有 run ID。CI 固定模型和预算的复现入口见 [真实模型实验](docs/live-experiments.md)。此前源码推理阶段的 19 次真实尝试保存于 [1.5B](experiments/round-1-1_5b)、[3B](experiments/round-2-3b)、[文件工具修复后的 3B](experiments/round-3-file-tools) 和 [16 步单案例回归](experiments/file-tools-v3)：全部未 Submitted，有公开检查的 10 次均未交付成功。这些历史失败完整保留，不与之后的 Docker 实验混成一个策略对照。

新增 [7B Docker 真实交付实验](experiments/closure-proof-v1/analysis.md) 保留了一个确定失败：24 次重复误用 Skill 工具、空补丁、未提交、独立公开验收失败。实际动作审计定位了错误，随后修复 Skill/源码读取指引，并加入依据真实失败记录生成的重复调用反馈；协议测试不能代替后续模型实测。未取得真实模型的官方解题成绩，不能据此宣称成功率提升。

工程验证已在 [21f1f5f 的 CI](https://github.com/chendi-Shi/repo-delivery-agent/actions/runs/38016237676) 中通过 Windows 和 Linux 各 230 项测试，并完成真实 Docker 运行与崩溃恢复。[官方容器合同检查](experiments/official-harness-contract/summary.json) 使用零 LLM 调用的控制补丁，实际评分器报告补丁应用成功、问题未解决、无基础设施错误；它验证评分链路，不进入模型成绩。此前真实模型的 [官方门禁](experiments/official-gate-v02/summary.json) 为 `no_submissions`。各阶段完整证据见 [验证记录](docs/validation.md) 和对应 [GitHub Actions](https://github.com/chendi-Shi/repo-delivery-agent/actions)。

## 产物与岗位对应

```text
spec.json                   任务/配置/Skills/代码/upstream指纹
runtime.json                模型/环境与恢复绑定
workspace/                  独立编辑工作区
agent/history.sqlite        完整历史与原始观察
agent/request-views.jsonl   请求、上下文选择、provider usage
agent/checkpoint.json       原子恢复状态与未知操作
agent/trajectory.json       真实DefaultAgent轨迹
model.patch                 实际文件改动
validation/                 干净checkout独立验证
result.json                 提交、patch、公开检查与官方状态
knowledge-ingestion.json    独立KB导入状态
```

| JD 方向 | 可展示内容 |
|---|---|
| AI Native 研发工具 | 公开需求→真实仓库修改→补丁→独立验收 |
| Agent 框架与模型适配 | 成熟 runtime 扩展、直接 Ollama adapter、真实接口契约 |
| Skills、上下文与知识库 | 渐进披露、原始证据、混合检索、namespace 和来源验证 |
| 编排调度 | Coordinator/worker、租约、心跳、旧 worker 拒绝、取消与受控恢复 |
| 评测与持续调优 | 真实任务冻结、失败分母、实际 usage/耗时、补丁/报告身份 |
| 系统设计 | 执行边界、崩溃窗口、未知状态、可复现实验 |

## 依据与边界

复用 [mini-swe-agent 固定源码](https://github.com/SWE-agent/mini-swe-agent/tree/04d809ceab9df28f9adaed044884180159172930)，参考 [OpenHands 事件设计](https://github.com/OpenHands/software-agent-sdk/tree/33044182505244749928644a4643368a9e998ee7)、[Agent Skills 格式](https://agentskills.io/specification) 和 [固定官方 harness](https://github.com/SWE-bench/SWE-bench/tree/02e7a74ffd0b707aab73d203fe87bdc7c76afc8e)。复用范围和原创部分见实现说明。

上下文预算是保守 UTF-8 字节估算，实际 token 来自 provider；缺失 usage 保留不完整标记。BGE+RRF 是固定混合排序，没有训练学习排序模型。SQLite coordinator 是单持久协调器，尚无多副本或多租户权限体系。artifact hash 证明内容身份，不是远程执行签名。

MIT License。上游依赖及许可证归各项目所有。
