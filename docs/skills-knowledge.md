# Skills 与跨任务知识库

这两个能力分别解决“如何执行某类研发任务”和“哪些过去的经验有可回查证据”。Skills 是操作者选择的工作方法；知识记录是任务数据，并带有来源、适用仓库和验证状态。它们不直接授予宿主执行权限。

## Skills

`SkillRegistry` 发现指定目录的直接子目录，读取 `SKILL.md` 的 `name` 和 `description`，并冻结资源列表与 SHA-256。默认包含两个项目内置 Skills：

- `python-regression`：Python 缺陷复现、回归测试和单次迭代的注意事项；迭代器细节放在按需读取的 reference。
- `patch-delivery`：提交前检查实际文件改动、公开检查与独立验证状态。

格式参考 [Agent Skills specification](https://agentskills.io/specification) 和 [Anthropic Skills 示例](https://github.com/anthropics/skills)。采用三阶段披露：发现只提供名称/描述；Agent 主动读取 `SKILL.md`；需要细节时再读取 supporting resource。

```python
from delivery_agent.skills import SkillRegistry

skills = SkillRegistry()  # 或 SkillRegistry(["./my-skills"])
print(skills.list())
print(skills.read("python-regression"))
print(skills.read("python-regression", "references/iterators.md", limit=2000))
```

Agent toolbox 对应 `delivery-skill list` 与 `delivery-skill read`；具体命令参数以运行时注入的工具说明为准。`read` 每页最多 4000 字符。注册时每个资源最多 64 KiB，一个 skill 最多 64 个文件、总计 512 KiB，registry 最多 64 个 skills。读取拒绝绝对路径、目录穿越、符号链接逃逸、清单外文件，以及注册后被修改的资源。

标准库 parser 支持所需的纯文本/引号 scalar 和 description 的 `|`/`>` block；不是完整 YAML parser。未知的可选 metadata 不参与权限配置。此 registry 只接受 UTF-8 文本资源，不自动执行 `scripts/`，不解释 `allowed-tools` 为权限授权。二进制资产和任意复杂 YAML 不在这个文本 toolbox 的范围内。

## 持久知识库

`KnowledgeStore(path, namespace)` 使用 SQLite WAL、FULL synchronous、busy timeout 和短写事务。每次操作创建并关闭独立连接，多个进程可以读取或写入同一个数据库。主记录与 FTS 索引在同一个事务更新，避免重复、过期的搜索副本。

每条记录包括 `source_id`、来源类型/引用、任务 ID、仓库、固定 commit、内容 hash、验证证据、创建/更新时间、过期时间，以及可选 embedding 的模型空间和维度。namespace 参与来源 ID 的 hash；所有读写和检索都限定当前 namespace。到期或显式失效的记录不进入搜索；直接读取仍可查看来源，但返回 `expired`/`stale` 和有效 `verified=False`。

```python
from delivery_agent.knowledge import KnowledgeStore

kb = KnowledgeStore("data/knowledge.sqlite", namespace="my-project")
imported = kb.ingest_run("runs/example")
hits = kb.search("generator exhaustion", verified_only=True, repo="owner/repo",
                 exclude_task_ids=["currently-evaluated-task"])
record = kb.read(imported["source_id"], offset=0, limit=2000)
```

`search` 默认只检索尚未到期的 verified 记录，最多 8 条，每条 excerpt 最多 500 字符。`read` 分页最多 4000 字符，附带来源和验证元数据。Agent 暴露的工具是只读的 `delivery-knowledge search/read`；它不能将自己的笔记或最终回复写成 verified knowledge。

`exclude_task_ids` 同时过滤词项和语义候选，用于防止同一评测任务的先前补丁回流。做对照实验时应使用独立先验来源，并隔离实验 namespace；已经排除当前 task ID 仍不能代替对同源衍生任务和数据污染的检查。

### 导入如何确定 verified

`ingest_run` 导入操作者信任的普通仓库 harness 产物，核对：

1. 任务 fingerprint、`spec.json` 和结果 provenance 一致。
2. `result.model_patch` 与 `model.patch` 内容一致，patch SHA-256 一致。
3. `fixture=False`、真实 provider、submitted 状态、patch 独立应用成功。
4. 结果包含任务中全部公开检查，命令顺序一致，每项 return code 为 0 且 passed 为 true。

缺少上述验证的记录仍可保存，但状态为 unverified，默认搜索排除。导入正文只取公开需求、公开检查输出和补丁片段；Agent narrative、notes、trajectory 不作为验证。`official_resolved=True` 这个单独布尔字段不会被自动提升为官方认证。

这里的 hash 用于发现产物错配，不是签名，也不证明任意目录中的 JSON 不能被伪造。操作者必须信任导入来源的 harness；`put(verified=True, verification=...)` 是明确的操作者 API，不向 Agent 工具开放。verified 只表示这些公开检查通过，不保证新任务正确，也不等于 SWE-bench resolved。

### 可选语义检索

默认使用 [SQLite FTS5 BM25](https://www.sqlite.org/fts5.html)；查询词始终加引号并参数化，不接受用户拼接 FTS 操作符。标题权重为 2，正文权重为 1。

```python
from delivery_agent.knowledge import KnowledgeStore, OllamaEmbedder

embedder = OllamaEmbedder("bge-m3", "http://127.0.0.1:11434", timeout=60)
kb = KnowledgeStore("data/knowledge.sqlite", "my-project", embedder=embedder)
print(kb.index_pending())  # 为已有记录补充 vectors
print(kb.search("an iterator should not be traversed twice"))
```

adapter 通过 [Ollama `/api/embed`](https://docs.ollama.com/api/embed) 发起真实 HTTP 请求，设置 `truncate=False`；通过 `/api/tags` 固定模型 digest，每次嵌入前复查。模型 tag 变化、维度变化、非有限或零范数向量都会被拒绝。metadata 返回模型名、digest、维度和错误类型；不记录 provider 错误正文。

语义候选只来自同一 namespace、有效状态和相同模型 digest 的向量。向量做归一化后按 cosine 排序，再与 BM25 用 reciprocal rank fusion 合并：`1/(60+lexical_rank) + 1/(60+semantic_rank)`。这是固定、可解释的排序，不是训练出的学习排序。默认最多扫描 5000 个最近向量，截断会明确记录；适合项目级语料，不是大型 ANN 向量服务。

嵌入服务失败时，写入记录保留词项索引并记录 `embedding_error`；搜索回退到词项检索。`index_pending` 可以重试补齐。词项检索使用 `unicode61` tokenizer，没有额外中文分词器。不能据此声称已提升模型成功率；效果需要固定模型/任务/预算的对照实验。

## 验证

`tests/test_skills.py` 验证渐进读取、资源冻结、路径边界与内置 Skills。`tests/test_knowledge.py` 验证 BM25/语义融合、namespace 隔离、持久化、去重更新、过期/失效、三个进程同时写入，以及 run 产物错配与验证来源。真实 loopback HTTP server 验证 Ollama 协议和 mutable tag 拒绝；它验证协议，实际 bge-m3 演示另见实验报告。

可复现的真实 BGE 检索 smoke：`python scripts/semantic_smoke.py`，需要本机已有 `bge-m3` 和运行中的 Ollama。它只使用三个公开文档的独立摘要，记录保持 `verified=False`，显式检索这些参考记录；输出 `experiments/semantic-search.json`。查询 `Do not scan this collection twice because it gets depleted` 与语料没有词项命中，真实 bge-m3 向量检索将相关 iterator 记录排第一；namespace 隔离和当前任务排除也通过。

首次短查询 `one shot traversal` 的真实结果单独保留在 `experiments/semantic-search-initial.json`：词项仍然 0 命中，但 iterator 排第二、SQLite 排第一，严格的 top-1 smoke 未通过。随后使用更清楚的查询得到上述结果。两条探针都只是三篇语料的功能检查，不能估计检索总体质量，也不是 CI 模型打分或 coding-agent 成功率实验。实际嵌入空间固定为 digest `7907646426070047a77226ac3e684fbbe8410524f7b4a74d02837e43f2146bab`，维度 1024。
