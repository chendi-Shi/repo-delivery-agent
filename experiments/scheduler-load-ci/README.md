# GitHub Actions 调度验收：负载通过，常规 Windows CI 失败

本目录对应提交 `5fc6d0d1ece35cb252e5573998b02e863bda227c`。两平台调度负载报告均通过，每个平台各完成 1,000 个 fixture 负载任务和 5 个故障任务，使用单机上的 8 个 worker 进程，模型调用为 0。原 ZIP 已下载并与 GitHub artifact SHA-256 核对；逐任务记录重新通过严格消费者，延迟与吞吐也从原始时间戳重新计算。[完整收据](validation.json)

| 平台 | 原报告 | 负载吞吐 jobs/s | 领取至提交 p50 / p95 秒 | 整轮秒 |
| --- | --- | --- | --- | --- |
| Linux | [report](ubuntu/report.json) | 295.440 | 0.009713 / 0.042964 | 14.947 |
| Windows | [report](windows/report.json) | 56.447 | 0.024278 / 0.206002 | 52.609 |

吞吐分母是首个负载租约开始至最后一个负载提交的时间，包含首步屏障，排除入队、启动、回读和空闲退出。两台 CI runner 各自单独测量；这不是操作系统因果对照或多机调度实验。Windows 实际记录 5 个幂等完成回复；任务历史与数据库审计仍为零次重复接受，不能把重试回复当成重复交付。

**同一提交的常规 CI 没有全绿。** Linux 为 344 tests、0 skip，通过；Windows 为 344 tests、0 skip、1 failure：`test_two_independent_processes_claim_unique_jobs_and_heartbeat` 发现任务状态中仍有 `queued`。当时日志不足以确定根因，失败收据保存在 `validation.json`；后续修复与新 CI 必须另存，不能用本次独立负载通过覆盖它。Docker fixture 验收通过；常规旧补丁验收记录为 6 次尝试、0 次提交、0 次成功，不是模型交付成功证据。

每个平台目录包含原字节 `report.json`、`report.jobs.manifest.json` 和 17 个 UTF-8 文本片段，单片不超过 64 KiB。片段保留原始 LF/CRLF 换行，本身不是独立 JSON。在相应平台目录启动 Python 后可执行：

```python
from pathlib import Path
import hashlib
import json

manifest = json.loads(Path('report.jobs.manifest.json').read_text(encoding='utf-8'))
parts = []
for order, entry in enumerate(manifest['parts'], 1):
    assert entry['order'] == order
    content = Path(entry['path']).read_bytes()
    assert len(content) == entry['bytes']
    assert hashlib.sha256(content).hexdigest() == entry['sha256']
    parts.append(content)
original = b''.join(parts)
assert len(original) == manifest['original_bytes']
assert hashlib.sha256(original).hexdigest() == manifest['original_sha256']
assert len(json.loads(original.decode('utf-8'))) == 1005
with Path(manifest['original_filename']).open('xb') as output:
    output.write(original)
```

原 ZIP 保留在本机被 Git 忽略的 `runs/scheduler-load-ci-downloads`，manifest 记录其大小、SHA、artifact ID 及来源提交。发布内容不包含临时下载 URL、HTTP 认证信息、租约令牌、私有临时路径或原始 CI 日志。
