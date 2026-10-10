# 本机调度负载与故障验收 v2

本目录保存原始 `report.json` 及逐任务证据的字节级分片。原 `runs/scheduler-load-local-v2` 文件未修改。1,000 个负载任务和 5 个故障任务均可逐条复核；验收为单机真实 HTTP/SQLite 与独立进程，业务执行是 fixture，模型调用为 0。

原始逐任务 JSON 为 **1,111,114 字节**，超过 1 MiB，因此分成 **17 个 UTF-8 文本片段，每个不超过 64 KiB**。片段不是独立 JSON，必须按 [manifest](report.jobs.manifest.json) 顺序逐字节拼接。CRLF 换行保留，不能使用会改变换行或额外插入分隔符的文本拼接工具。[原始报告](report.json) 内的 `report.jobs.json` 指拼接后得到的文件。

在本目录启动 Python 后，执行以下代码即可检查所有分片并重建原文件；输出使用独占创建，已有文件不会被覆盖：

```python
from pathlib import Path
import hashlib
import json

manifest = json.loads(Path('report.jobs.manifest.json').read_text(encoding='utf-8'))
chunks = []
for index, part in enumerate(manifest['parts'], 1):
    assert part['order'] == index
    content = Path(part['path']).read_bytes()
    assert len(content) == part['bytes']
    assert hashlib.sha256(content).hexdigest() == part['sha256']
    chunks.append(content)
original = b''.join(chunks)
assert len(original) == manifest['original_bytes']
assert hashlib.sha256(original).hexdigest() == manifest['original_sha256']
assert len(json.loads(original.decode('utf-8'))) == manifest['row_count']
with Path(manifest['original_filename']).open('xb') as output:
    output.write(original)
```

原始报告的 `source_commit` 为 null，本次发布没有补造提交号。发布时检查发现：报告中 scheduler、server、worker 和负载脚本的四个运行哈希仍匹配工作区；`delivery_agent/repository.py` 已发生后续修改。两个版本的哈希均记录在 manifest。因此这份历史结果不能代替对所有当前源码的验证。三个新增代码文件的发布时哈希也记录在 manifest；生产核心文件未在打包过程中修改。

后续 CI 的两平台结果应作为独立证据保存，不能替换本次本机测量，也不能把此处结果概括为多物理机器或生产规模证明。仓库 `.gitattributes` 的 `experiments/** -text` 用于保留证据原始换行和哈希。
