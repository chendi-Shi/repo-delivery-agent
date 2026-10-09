# 真实模型产物的独立仓库验收

`scripts/grade_experiments.py` 验证保存下来的真实 Ollama 模型补丁。它从固定 commit 创建全新 Git checkout，检查并应用实际 patch，再在独立 Docker 容器中执行任务定义的公开检查。模型推理过程中若使用 `files` backend，只能编辑源码，不能提前声称测试成功；本 verifier 补齐独立执行证据。

```bash
python scripts/grade_experiments.py \
  --manifest experiments/repository-ablation.json \
  --pull-images --command-timeout 90 \
  --output runs/repository-verification.json
```

GitHub CI 的 `real-model-patch-verification` job 会运行上述检查，上传 `real-model-public-check-verification` artifact。不执行 Agent 生成的宿主 shell 命令；公开检查仅通过受限的 `DockerSession` 运行。Verifier 输出记录实际镜像 ID、补丁 hash、任务 hash、输入证据 hash和公开检查摘要。

## 输入与检查边界

manifest 使用 schema 1，包含 `kind: real_model_repository_ablation`、`runs`，以及可选的小样本 `limitations` 列表。每个 run 的 `task`、`patch`、`evidence` 路径必须位于 manifest 所在的 experiments 目录中，禁止绝对路径、向上越界和 symlink。

任务按严格 `RepoTask` 校验；gold patch 等答案字段不能混入。模型证据必须 `fixture: false`，provider 为 `ollama_native`，补丁 hash 必须匹配文件。只有实际 `exit_status: Submitted` 且具备模型 digest 的记录可标记提交成功。provider 初始化失败的未提交尝试允许 `model_digest: null`，报告明确标记身份未解析，仍保留在尝试总数里。

`context_requests` 可以是请求次数，也可以是有界的上下文审计 metadata 列表。完整 provider 输出不作为 verifier 的可执行输入；身份和 usage 元数据是运行侧记录，公开测试结果是 verifier 独立产生的证据，两者不混为一项。

## 成绩口径

只有模型按协议提交、补丁能应用、全部公开检查通过，`delivery_success` 才为真。非提交尝试即使留下了碰巧通过检查的 partial patch，也不能算交付成功。所有 failed、stopped 和 submitted 尝试都进入 `attempts` 分母，并按 profile 分组报告。

模型失败、无法应用的 patch、断言失败和已确认清理的命令预算耗尽属于实验结果，脚本完成后返回 0，并把失败保存在 JSON。镜像拉取失败、checkout 失败、Docker 不可用、清理无法确认和输入证据损坏属于基础设施或产物完整性失败，返回 2；未完整验收时交付率为 `null`，不能把未知检查记成成功或失败来得出确定成绩。

这套检查是公开仓库需求的交付验收，不是官方 SWE-bench 隐藏测试评分。报告中的 `official_resolved` 始终为 `null`；官方 benchmark 应通过项目单独的 official harness 流程取得。小样本结果不能直接外推到通用编程能力，也不能单靠 `baseline` 与 `retrieval` 的差异推断检索的因果收益。
