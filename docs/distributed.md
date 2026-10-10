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

任务文件只包含 `task` 和 `config`。`task` 使用与 `delivery-agent run` 相同的公开 RepoTask 格式；`config` 接受模型标识、profile、provider、backend、Ollama 协议和有界预算，禁止远程提供 host 命令、输出路径、模型 endpoint 或本地技能/知识库路径。`provider` 可为 `litellm`、`ollama_native` 或 `codex_cli`；`backend` 可为 `docker` 或 `files`，后者仅允许受限源码编辑，测试仍由独立 Docker verifier 完成。Codex 可通过 `config.reasoning_effort` 选择 `low`（默认）、`medium`、`high` 或 `xhigh`；可执行文件只由 worker 本地 `--codex-binary` 配置，认证复用该 worker 的已有登录状态。

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

`config.cost_tracking` 可省略，或在 LiteLLM provider 下显式设为 `default` / `ignore_errors`。前者计价失败即停止，后者继续执行并将未知费用保留为 null；美元 cost_limit 仅约束已知费用，无法限制未知账单。直接 Ollama 和 Codex provider 保持未定价，不接受显式模式。Codex CLI 不提供本适配器可设置的 sampling seed 或输出 token 上限，这两个输入预算字段不应解读为该 provider 的实测控制；所选消息仍受上下文预算约束。服务 endpoint、模型认证和可选的上游价格表均在 worker 本地配置，任务 JSON 不携带凭据。详细的自定义模型命令见 README。

状态为 `queued → leased → succeeded/failed`，失败且尚未用尽预算时返回 `queued`；取消变为 `cancelled`。`succeeded` 表示 executor 按提交协议完成，不能替代 `patch_valid`、公开测试或 `official_resolved`。这三个质量指标仍来自交付和评分系统。

- SQLite `BEGIN IMMEDIATE` 在一个事务中完成 claim 和 attempt 创建，防止两个 worker 同时领取同一代任务。
- 每次领取增加 generation，并生成新的随机 lease token。心跳、结果提交同时校验 job、worker、generation 和 token。
- HTTP client 为一次逻辑领取生成 `request_id`，遇到传输结果未知时沿用它。coordinator 在分配事务中持久化该编号；同一次领取仍有效时返回原任务、原 generation/token 并续租，不新增 attempt。编号绑定 worker，已完成、已取消或已过期的旧编号只能返回空结果，不能领取下一个任务。拿到明确响应后，下次领取使用新编号；因此完成响应未知也不会触发同一代任务重新执行。新表随已有数据库启动时创建，旧版省略 request_id 的调用仍可用。
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

新增的 `scripts/scheduler_load.py` 在每个平台启动一个真实 HTTP coordinator、SQLite 数据库和 8 个独立 worker 进程，提交 1000 个普通任务及 5 个故障任务。executor 是可信测试替身，LLM 调用数为 0；每个 CI job 只有一台宿主，不能把数字描述为物理多机规模或模型交付吞吐量。

验收逐一查询全部 1005 个已知 job ID，并在进程停止后只读核对 SQLite。普通任务必须全部只领取一代、成功确认恰好 1000 次；缺失、额外任务、重复接受结果均为 0。故障过程包括杀死 worker 后等租约过期并重领，在新一代仍持有租约时拒绝旧代提交，重复结果幂等而不同结果冲突，队列中与运行中取消，以及杀死并重启 coordinator 后恢复原数据库和 URL。最终预期为 1003 个成功、2 个取消。

```bash
python scripts/scheduler_load.py --jobs 1000 --workers 8 \
  --lease-seconds 30 --timeout-seconds 300 --output runs/load-new/report.json
```

输出目录必须新建。报告包含实际参数、平台、进程、源码身份、各故障阶段、完整任务 readback 的 SHA256 和调度延迟。归档中的大 readback 按原始 UTF-8 字节分片，manifest 保留片段及重组后的 hash，避免截断日志造成只检查部分任务。

验证暴露了四个不同问题，分别处理：

1. 租约时间曾在获得 SQLite 写锁前计算。真实锁等待回归证明它可能让新租约在事务提交前已过期，也可能错误接受过期心跳/结果。现在在 `BEGIN IMMEDIATE` 获得锁后取时间；[修复前后证据](../experiments/scheduler-lease-clock)保留。
2. 5 秒租约在 Windows 1000 任务负载下仍不足。两次失败分别出现重新领取/缺少确认和一个最终失败任务；完整失败未删。负载配置改为 30 秒后，Linux/Windows 均通过原有严格单代与确认断言。这里是配置调整，不能声称 5 秒负载已经可靠；小规模心跳测试仍验证任务超过初始租约时续租。[负载及历史失败](../experiments/scheduler-load-ci-clock-fixed)包含各次参数与原始报告。
3. Windows 全量测试暴露错误令牌 POST 偶发连接中断，预期的 401 未送达。分段发送真实请求体可复现：修复前 5/5 为 WinError 10053，修复后 5/5 收到完整 401，均未创建任务。现在先返回并 flush 401，再只对无歧义、最多 64 KiB 的请求体做最多 250 毫秒的传输清理；不解析、不分派，TLS 使用已解密的流。超限、重复 Content-Length 或 Transfer-Encoding 不走清理。[复现证据与方法](../experiments/scheduler-load-ci-clock-fixed/http-rejection-recovery)和真实 HTTP/TLS、慢请求、歧义长度回归一起保留。
4. 随后 30 秒租约再次负载测试，Windows 一个任务仍发生了重新领取，尽管 1000 个任务最终成功且确认完整，严格单代验收仍失败。受控 HTTP 故障复现证明：领取事务提交后丢失响应，旧 client 重试无法取回已分配的任务，只能等租约过期。现在用上文的持久化领取编号恢复同一次分配；继续延长租约无法解决这个协议缺口。原始失败与修复后的验收分别保留，不用后一次成功覆盖前一次失败。

以下两 worker 集成是额外的长任务续租检查。

`tests/test_scheduler.py` 除事务边界测试外，还真正启动两个 Python worker 子进程，通过 loopback HTTP 执行 register、claim、heartbeat 和 complete。六个任务运行时间长于初始租约，验证心跳续租且每个任务仅领取一代。另有独立进程领取后退出的恢复测试、运行中取消观察测试、旧 token 拒绝和重复提交幂等测试。

这里的集成 executor 是仓库内可信测试代码，不调用 LLM 或 Docker，也不能由远程任务指定；因此这些结果证明调度协议可运行，不是模型成功率。实际 executor 的取消容器和构造竞态另有定向测试，Docker 交付能力由项目的真实 Docker CI 检查覆盖。

```bash
python -m unittest discover -s tests -p test_scheduler.py -v
```
