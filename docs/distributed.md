# 多进程、多机器调度

调度系统已经可以运行多个独立 worker 进程，也允许 worker 位于不同机器。一个 HTTP coordinator 独占其本地 SQLite 数据库；worker 通过 API 领取任务、续租和提交，不依赖共享文件系统。模型密钥与模型 API endpoint 保留在 worker 本地。

## 启动与任务提交

先在 coordinator 和 worker 的环境中设置同一个至少 32 字符的随机 `DELIVERY_SCHEDULER_TOKEN`。令牌由运行环境提供，不写入任务 JSON、SQLite 或启动日志。不要把下面的占位符当成真实密钥。

```bash
export DELIVERY_SCHEDULER_TOKEN='<由部署环境注入的随机令牌>'
python -m delivery_agent.scheduler_server \
  --db runs/coordinator.sqlite --host 127.0.0.1 --port 8765 --lease-seconds 30
```

在 Windows PowerShell 中对应的环境设置是 `$env:DELIVERY_SCHEDULER_TOKEN = '<随机令牌>'`。默认仅监听 loopback。跨机器部署时显式指定 LAN 地址，例如 `--host 0.0.0.0`，通过私有网络和防火墙限制访问；HTTP bearer token 本身不能保护网络传输。支持直接提供 `--tls-cert certificate.pem --tls-key key.pem`，worker 可用 `--ca-file ca.pem` 验证自签发证书，也可以在认证反向代理后部署。此实现不宣称适合直接暴露到公网：目前为可信部署环境使用单个共享令牌，尚无多租户权限隔离。

任务文件只包含 `task` 和 `config`。`task` 使用与 `delivery-agent run` 相同的公开 RepoTask 格式；`config` 接受模型标识、profile、provider、backend、Ollama 协议和有界预算，禁止远程提供 host 命令、输出路径、模型 endpoint 或本地技能/知识库路径。`provider` 可为 `litellm` 或 `ollama_native`；`backend` 可为 `docker` 或 `files`，后者仅允许受限源码编辑，测试仍由独立 Docker verifier 完成。

```json
{
  "task": {
    "instance_id": "public-repo-task-1",
    "repo": "owner/project",
    "base_commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "problem_statement": "填写真实公开需求",
    "image": "python:3.12-slim",
    "public_checks": ["python -m unittest discover"]
  },
  "config": {
    "model": "ollama_chat/qwen3:4b-instruct",
    "profile": "retrieval",
    "max_steps": 20,
    "wall_seconds": 900,
    "context_tokens": 16384,
    "max_output_tokens": 2048,
    "cost_limit": 3.0
  }
}
```

该 JSON 中的 SHA 只是格式示例，运行时必须替换为目标仓库真实 commit。提交、查询和取消使用同一客户端：

```bash
python -m delivery_agent.scheduler_worker --url http://127.0.0.1:8765 \
  --submit task-payload.json --job-id public-repo-task-1 --max-attempts 3
python -m delivery_agent.scheduler_worker --url http://127.0.0.1:8765 --list
python -m delivery_agent.scheduler_worker --url http://127.0.0.1:8765 --status public-repo-task-1
python -m delivery_agent.scheduler_worker --url http://127.0.0.1:8765 --cancel public-repo-task-1
```

启动两个独立 worker，可位于同一机器，也可使用私有网络的 coordinator HTTPS URL：

```bash
python -m delivery_agent.scheduler_worker --url http://127.0.0.1:8765 \
  --worker-id worker-a --work-root runs/worker-a --api-base http://127.0.0.1:11434
python -m delivery_agent.scheduler_worker --url http://127.0.0.1:8765 \
  --worker-id worker-b --work-root runs/worker-b --api-base http://127.0.0.1:11434
```

worker 默认调用实际 `cli.run_repository`，需安装 `.[agent]`、可访问模型服务，且 Docker 已启动、镜像已准备。公开检查命令仅在任务容器内执行。每个 worker 同时运行一个任务，水平扩展通过部署更多具有唯一 worker ID 的进程完成；`--max-jobs` 和 `--idle-seconds` 可让批处理 worker 有界退出。

worker 可通过本地 `--skills paths...`、`--knowledge-db path`、`--knowledge-namespace name`、`--embedding-model model` 使用技能和持久知识库。embedding endpoint 独立使用 `--embedding-api-base`（默认 `http://127.0.0.1:11434`），可以与编程模型 endpoint 不同。不同机器可维护各自的知识库；调度 coordinator 不共享或传送知识库数据库。这些本地配置参与 worker configuration digest，远程任务不能选择宿主资源路径。

## 状态、恢复与提交语义

状态为 `queued → leased → succeeded/failed`，失败且尚未用尽预算时返回 `queued`；取消变为 `cancelled`。`succeeded` 表示 executor 按提交协议完成，不能替代 `patch_valid`、公开测试或 `official_resolved`。这三个质量指标仍来自交付和评分系统。

- SQLite `BEGIN IMMEDIATE` 在一个事务中完成 claim 和 attempt 创建，防止两个 worker 同时领取同一代任务。
- 每次领取增加 generation，并生成新的随机 lease token。心跳、结果提交同时校验 job、worker、generation 和 token。
- 租约过期后，下次 claim/read/list 会回收任务；未超出上限时重新排队，否则标记失败。coordinator 重启不会丢失任务、attempt 记录或已接受结果。
- 旧 worker 的结果即使在新 worker 完成之前返回，也会被拒绝。已接受的相同 generation、相同结果 hash 提交可安全重试；相同租约提交不同结果会冲突。
- 不承诺模型调用 exactly-once：机器断电或网络分区可能导致任务重新执行。承诺的是当前租约的受控提交，以及相同结果的提交幂等。
- 取消或无法续租时，worker 设置取消事件，主动关闭此任务登记的 Docker 容器，并拒绝后续命令；构造中的容器在有界启动返回后立即检查取消并清理。已经发送给 provider 的请求无法撤回，会受其请求 timeout 约束。执行数据隔离且失效租约无权提交。
- 强制杀死 worker 进程后，coordinator 能恢复任务，但它无法替远程主机确认 Docker 清理。部署应使用进程管理器和主机级孤儿容器清理策略；本系统不会把这类情况宣称为完全自动的远程容器恢复。

coordinator 的数据库应位于本机持久磁盘，不能让多个 coordinator 在不同机器通过网络盘共享同一个 SQLite 文件。当前是一台持久 coordinator + 多台 worker 的结构，并非多副本 coordinator 或 Raft 系统。

## 结果与证据

提交携带有界 patch、结果摘要及已知 artifact 的 SHA-256 manifest。patch 上限 8 MiB、结果摘要上限 512 KiB；HTTP 请求上限 64 MiB，以容纳 JSON 转义后的最大补丁。原始 provider trace、完整工具输出和 worker 的绝对文件路径不会直接上传。

结果中记录 worker hostname、本地运行配置 digest，以及交付层记录的实际镜像 ID、本地模型 digest（若 provider 支持）。查询任务可以取回补丁及 manifest，而不用访问 worker 的磁盘；完整证据保留在 worker 输出目录，可按 digest 独立核对。Artifact digest 证明内容身份，不代替 provider 行为或评分事实。

## 实际验证

`tests/test_scheduler.py` 除事务边界测试外，还真正启动两个 Python worker 子进程，通过 loopback HTTP 执行 register、claim、heartbeat 和 complete。六个任务运行时间长于初始租约，验证心跳续租且每个任务仅领取一代。另有独立进程领取后退出的恢复测试、运行中取消观察测试、旧 token 拒绝和重复提交幂等测试。

这里的集成 executor 是仓库内可信测试代码，不调用 LLM 或 Docker，也不能由远程任务指定；因此这些结果证明调度协议可运行，不是模型成功率。实际 executor 的取消容器和构造竞态另有定向测试，Docker 交付能力由项目的真实 Docker CI 检查覆盖。

```bash
python -m unittest discover -s tests -p test_scheduler.py -v
```
