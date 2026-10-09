# 检查点与断点恢复

恢复的目标是继续一个已经确定执行到哪一步的 trial，并保留原始预算。
`checkpoint.json` 是可信宿主目录里的恢复源，不放进 Agent 可写挂载。
它原子写入、fsync，并有 SHA-256 完整性校验。校验可以发现损坏和意外
编辑，不是数字签名，也不能抵抗能改写宿主文件并重算 hash 的攻击者。

## 状态与绑定

每个检查点保存完整消息、稳定 evidence source ID、任务笔记、Agent 调用
次数与成本、连续格式错误次数、provider token usage、原始启动时间、
上下文配置和当前 operation transition。原始启动时间不重置，恢复期间
的停机时间也计入 wall-time 限制。已用 step/cost budget 不会因恢复清零。

恢复必须匹配任务、模板变量、Agent 配置、上下文配置、模型配置、固定
mini-swe-agent 版本和全部 `delivery_agent/*.py` 的代码指纹。CLI 还应提供
实际 image ID、模型 digest、workspace、base commit、container ID 和任务
指纹作为 `recovery_binding`，并在重新接入环境前核对这些身份。

同一个 trial 的本地 writer 使用操作系统锁。两个进程不能同时推进。
进程崩溃时锁由 OS 释放；不会留下需要手工删除的 PID 锁。此锁约束本机
进程，不能作为多主机网络文件系统上的分布式锁使用。

## 恢复协议

模型和工具操作都有三个阶段：

1. 在真正调用前保存 `pending`，其中有 request hash 和操作序号。
2. 返回已知响应后保存完整结果及 usage，然后清除 pending。
3. 将结果加入真实 DefaultAgent 消息轨迹后提交下一状态。

检查点位于阶段 2 时，恢复消费保存的模型响应或工具输出。不会再次调用
provider，也不会重新执行已经完成的命令。一个 assistant 含多条 action
时，已完成 action 的结果按原顺序回放，只有尚未执行的 action 才会执行。
上游 `Submitted`、`FormatError` 等确定的控制流结果同样可以保存和恢复。

SQLite 历史与 JSON 检查点有独立事务，进程可能在两者之间崩溃。恢复以
hash 验证的 checkpoint 为准，在一个 SQLite 事务内重建消息索引及笔记，
避免重复 source ID 或把未提交片段当成确定历史。

## 未知结果不自动重放

如果进程在远程模型请求或容器命令期间崩溃，checkpoint 会留下 pending。
此时无法知道模型是否已计费、命令是否已改动文件。默认恢复报告
`AmbiguousRecovery`，并且不发起新的模型请求或命令。

显式 `recovery_policy="retry"` 会在 checkpoint 的 `recovery_decisions`
留下操作和风险确认记录，然后重试该未知操作。它可能产生重复计费或
重复副作用。丢失响应的模型请求保留在尝试计数中，并记录
`unknown_requests`；不能假装成本和 token usage 完整。另一种策略是创建
新的 trial，保留旧 trial 的失败证据，重新从固定 commit 开始。

容器已经消失、挂载改变、镜像改变或 HEAD 改变时，不能声称恢复的是同
一次执行。应重新验证环境身份，或建立新 trial。工具命令由宿主记录；
它们的输出缓存仅对应同一 action hash，不能拿旧结果满足新命令。

## Docker 暂停、接管和清理

```python
# 初次启动后冻结身份，写入 trusted spec/checkpoint。
identity = environment.identity(base_commit)

# 在已知检查点暂停时，原对象放弃后续执行和自动删除。
environment.preserve()
environment.close()  # 保留容器

# 新进程接管。校验完成前不会执行 Agent 命令。
resumed = DockerSession.attach(workspace, identity, inspect_timeout=30)
try:
    # 将 resumed 传给 run_agent(..., resume=True)。
    pass
finally:
    resumed.close()  # 新对象默认承担销毁责任
```

重新接入要求完整 container/image ID、同一 workspace 和 HEAD、运行且未
暂停的容器，以及原来的源码 RW/.git RO 挂载。它还核对真实 attached
networks、network none、read-only root、capabilities、no-new-privileges、
PID/内存/CPU 限制、用户、工作目录和临时目录。额外 volume/bind 被拒绝。
Windows Docker Desktop 已知的宿主盘符映射也按完整路径核对。

`preserve()` 后原对象不能继续发命令；它不是延长运行预算，也不会改变
checkpoint 的 pending 状态。`close(force=True)` 只供明确拥有该 session
的调用者执行最终清理。实际崩溃后没有 finally，因此依赖已持久化的
container ID 重新接管或清理，不能枚举并删除其他任务的容器。

`python scripts/docker_recovery_smoke.py --output runs/docker-recovery.json`
在真实 Docker 中运行确定的计数器修改，子进程在已完成 action 的结果
落盘后真正退出。另一个子进程重新接入同一容器并恢复真实 DefaultAgent，
检查计数器仍为 1、调用/成本/历史连续，并在提交后删除测试自己的容器。
它需要安装 `.[agent]` 且预先存在 `python:3.12-slim` 镜像。

## Python 接口

```python
from delivery_agent.checkpoint import get_pending, inspect_checkpoint
from delivery_agent.upstream import run_agent

# 只读检查，不会接入容器，也不会发模型请求。
record = inspect_checkpoint("runs/trial/checkpoint.json")
pending = get_pending("runs/trial/checkpoint.json")

# model/environment 必须来自重新核验过的相同运行身份。
result = run_agent(
    model, environment,
    task=original_task,
    agent_config=original_agent_config,
    output_dir="runs/trial",
    recovery_binding=original_runtime_binding,
    resume=True,
    recovery_policy="stop",
)
```

恢复通过真正的 mini-swe-agent `DefaultAgent` 执行，继续其消息、查询、
action 和异常协议。测试使用确定的本地模型/环境 fixture，并在独立子
进程中通过 `os._exit` 真正崩溃。这证明恢复协议；不属于真实模型效果
评测，也不替代真实 Docker 重新接入测试。
