# 真实仓库交付、知识复用与失败核算案例

这个项目已经跑通“真实需求 → 模型修改仓库 → 实际补丁 → 独立验收 → 已验证知识入库 → 新任务读取”的工程链路。固定的十个目标尝试全部取得独立验收结论：**4 次正式交付成功、6 次失败、0 次验收未知**。另行预登记的两次补充尝试均成功，单独统计，没有替换原失败。原始证据、失败、用量未知和离线导出恢复均保留，见[原十轮汇总](../experiments/codex-transfer-v1/summary.json)和[补充两轮汇总](../experiments/codex-transfer-v1/supplement-summary.json)。

结果最容易被误读的地方是：六个 `last` 目标的补丁**全部通过两项冻结公开检查**。baseline 两轮和带源知识的 retrieval 两轮均按协议 `Submitted`，正式交付各为 2/2；空库 retrieval 两轮也留下了通过检查的补丁，却在 24 步上限前没有完成提交，正式交付为 0/2。因此不能把这批结果写成“知识把代码正确率从 0 提升到 100%”。两次带知识运行确实读到了非空源记录；是否因此改变停止或提交行为，当前小样本无法作因果判断。baseline 也是 2/2，结果同样不能支持“知识优于 baseline”。

这里要解决的研发问题是：开发工具怎样把模糊需求转为可执行任务，控制 Agent 的执行和上下文，保留能回查的证据，并区分“说完成了”“已提交补丁”“补丁通过公开检查”“官方任务解决”四件事。项目选用 [mini-swe-agent 2.4.6](https://github.com/SWE-agent/mini-swe-agent/tree/04d809ceab9df28f9adaed044884180159172930) 的真实 `DefaultAgent` 和模型/环境协议，新增交付、上下文、Skills、知识、恢复和调度模块。上游循环不是本项目原创；增强实现与上游边界见[实现说明](implementation-walkthrough.md)。

与岗位 JD 的关系可以具体落到这些模块，而不是仅仅出现几个技术名词：

| JD 方向 | 已实现的工程能力与证据 | 仍需验证的边界 |
| --- | --- | --- |
| AI Native 研发工具与基建 | 固定仓库任务输入、CLI/worker 提交、隔离执行、实际 patch、独立 grader、可回查运行产物；本案例真实走过整条链路 | 没有接入抖音业务、产品埋点或真实团队研发流程，不能宣称业务提效 |
| Agent 框架、编排、模型适配 | 继承真实上游 Agent；Docker 与受限 files 环境；LiteLLM、直接 Ollama、Codex CLI 适配；任务预算、取消、checkpoint 和 worker 租约 | 远端模型权重不可固定；网络故障时不承诺模型调用 exactly-once |
| 上下文工程与 Skills | 不改原始历史的预算视图、完整工具调用配对、来源 ID 与分页读取、Skills 清单及资源 hash、循环反馈 | 预算是明示的保守字节估算；不能据此声称最优上下文策略或普遍降低 tokens |
| 评测、知识库、持续调优 | 公开检查/官方评分分离；验证来源入库；FTS5/BM25 与可选向量检索；新私有 DB 对照、非空 read 审计、失败与未知用量完整核算 | 本批两次重复不足以证明知识提升交付率、代码质量或成本效率 |
| 问题拆解与迭代 | 从真实工具循环、Git/路径/原子写入故障定位到具体回归测试；调度验证包含租约竞争、故障恢复与完整 readback | 单机多进程验证不等于物理多机容量，更不是亿级用户或生产 SLA |

**一次任务如何运行。** 任务输入包含公开需求、仓库、完整 commit、执行镜像和公开检查。运行先冻结 spec、任务与代码身份，在独立 checkout 中执行。模型通过工具读、搜索和修改真实源码，补丁从实际文件系统生成，并在干净 checkout 重新应用。files 后端只解释受限文件操作，不执行模型生成的宿主 shell；该后端未运行内部测试时明确记录 `deferred`，测试交给独立 Docker verifier。最终成功必须同时满足真实提交协议及独立公开检查，不能根据模型的“完成”文字判断。

上下文层把“完整证据”和“当前模型请求”分开。原始消息、工具结果与 provider 输出持久保存；baseline、recent、retrieval 决定本次请求选入哪些完整轮次。system、原始任务和最新完整轮次受保护，工具调用与全部对应结果不能被拆开；保护内容也放不下时，在发模型请求前停止。被缩短的观察保留来源 ID，历史工具可以分页找回。Skills 先暴露名称、描述，再按需读 `SKILL.md` 和清单内资源；内容 hash 冻结后拒绝悄悄换内容，Skill 文本也不会扩大执行权限。

此前真实小模型运行曾反复读 EOF，或照抄帮助中的 `literal query` 与 `[limit 1..8]`。修复把示例改为可复制命令，范围放在解释文字里；EOF 明确说明没有后续内容；工具说明限定只适用于相应 toolbox 调用，正常仓库命令仍可使用。上下文视图根据持久原始记录中的真实执行返回码、命令和输出识别长度 1–3 的重复只读循环，追加有界提示，不改历史、不伪造动作、不强制给出任务答案。需求与公开检查放到参考指引之后，避免工具说明遮住实际任务。对应真实失败继续保留；这些通用修复本身不等于已证明成功率提升。

执行恢复与调度承担另一类问题。checkpoint 区分 pending、已保存 result 和已提交消息状态：已知结果可以回放，未确定的外部调用保留 unknown，不盲目声称没有计费。独立 worker 经 HTTP coordinator 领取任务，SQLite 事务、generation、lease token 和结果 hash 约束续租及完成提交，拒绝旧代结果覆盖新代。同一结果可以幂等重交；整个模型调用不保证 exactly-once。真实多进程负载、锁等待、取消和重启证据，以及共享令牌/单 coordinator 的限制，见[调度说明](distributed.md)。这里的调度 fixture 不调用模型，不能拿它的吞吐量当 Agent 交付吞吐量。

**知识从哪里来。** 本次源任务是 more-itertools 的 `first` 扩展。它实际进行了 15 次 Codex CLI 调用，报告输入 242,373 tokens、输出 1,996 tokens，共 244,369 tokens，仓库运行耗时 181.590 秒。公开动作审计记录了 1 次文件列表、2 次源码搜索、6 次文件读取、3 次 Skill 读取、2 次文件替换和 1 次提交。源经验由实际题面、实际补丁和实际独立检查输出生成，没有人工补写解题答案或虚构“成功经验”。

源任务原始运行记录中的 `patch_valid=false`、`complete=false` 均保留。独立 Docker 验收从冻结基线重新检出并应用补丁，记录 `patch_valid=true`，全部公开检查通过。入库元数据同时保存原运行的 `source_patch_valid=false` 与外部验收的 `patch_valid=true`；内部验证仍为 `deferred`。这是两个阶段的事实，不把后验结果改写成原运行已经内部测过。[源产物](../experiments/codex-transfer-v1/source)、[独立验收与门禁](../experiments/codex-transfer-v1/source-verification/source)和[机器生成源记录](../experiments/codex-transfer-v1/source-package.json)可以互相核对。

源入库前还检查目标泄漏：比较源补丁应用前后 `last`、`split_after` 的完整 AST，忽略位置等无关差异，检查动态目标 API 重新绑定；再对源补丁实际执行两个目标的新功能公开检查。目标定义未改变，两个目标检查仍失败，才允许源记录进入知识组。源补丁完整保留，不通过截掉可疑片段来“修成”合格知识。这个门禁约束直接提供冻结目标实现的泄漏，不声称消除了全部语义上的迁移信息；可迁移经验正是要观察的对象。

[入库脚本](../scripts/ingest_verified_delivery.py)核对任务、manifest、补丁、evidence、独立报告和 source guard 的 SHA 绑定，输出 `external_independent_public_checks` 类型记录。SHA 能发现内容错配，不能自行认证任意外来 `verified=true`；可信来源仍依赖对应工作流、提交、artifact 身份和实际执行的 verifier。知识层也有 namespace、expiry 和当前 task 排除，详情见[Skills 与知识设计](skills-knowledge.md)。

每个目标都有新的私有 SQLite DB。有知识组复制同一个已验证源输入，空组创建新空 DB；所有组的知识工具均可用，避免混入“有无工具”的差异。目标运行产生的记录不回流给其他目标，也不改原源 DB。公开 `knowledge-input.json` 冻结每轮输入正文和 origin，供独立消费者复核。

“读过知识”也不靠模型自述。公开动作审计必须同时证明：调用真实完成、返回码为 0、操作是 `delivery-knowledge read`、来源匹配冻结记录、完整命令长度与 SHA 一致，并且 offset/limit 对应不可变正文的非空切片。汇总器重算切片长度和 SHA。search、失败 read、EOF 空读取和截断命令都不能算有效读取；审计不完整且没有正向证据时为 unknown。本次两个知识组均通过该验证。它证明对应内容通过工具返回给模型，不证明模型采用了内容或由此获益。

**实验怎样固定。** [原计划](../experiments/codex-transfer-v1/plan.json)在执行前声明源任务、十个目标位置、分组和两次重复。模型为 `gpt-6-astra`、reasoning effort `low`；每轮 24 步、64,000 字节选取上下文预算、单次请求 120 秒、仓库运行 1,800 秒，最多两轮并发。64,000 是字节，不能写作 64K tokens；CLI 自身附加提示不在该预算内。适配器不声称远端执行了 sampling seed 或输出 token 上限。达到 24 步是真实停止原因，没有为了消除失败而增加原计划步数。

| 固定目标位置 | 实际 CLI 调用 | 输入 tokens | 输出 tokens | 仓库秒数 | 原始停止状态 | 非空源读取 | 独立交付 |
| --- | ---: | ---: | ---: | ---: | --- | --- | --- |
| last-baseline-empty-r1 | 20 | 360,796 | 2,385 | 311.062 | Submitted | 否 | 成功 |
| last-baseline-empty-r2 | 21 | 360,697 | 2,524 | 285.438 | Submitted | 否 | 成功 |
| last-retrieval-empty-r1 | 24 | 416,716 | 2,754 | 364.424 | LimitsExceeded | 否 | 失败；补丁检查通过但未提交 |
| last-retrieval-empty-r2 | 24 | 415,767 | 2,658 | 435.722 | LimitsExceeded | 否 | 失败；补丁检查通过但未提交 |
| last-retrieval-verified-r1 | 23 | 478,892 | 2,910 | 308.725 | Submitted | 是 | 成功 |
| last-retrieval-verified-r2 | 20 | 379,702 | 2,506 | 382.349 | Submitted | 是 | 成功 |
| split-baseline-empty-r1 | 1 | 未知；已知小计 0 | 未知；已知小计 0 | 67.442 | CodexProtocolError | unknown | 失败 |
| split-baseline-empty-r2 | 1 | 未知；已知小计 0 | 未知；已知小计 0 | 66.187 | CodexProtocolError | unknown | 失败 |
| split-retrieval-empty-r1 | 1 | 未知；已知小计 0 | 未知；已知小计 0 | 69.793 | CodexProtocolError | unknown | 失败 |
| split-retrieval-empty-r2 | 1 | 未知；已知小计 0 | 未知；已知小计 0 | 65.245 | CodexProtocolError | unknown | 失败 |

十个目标共确认 136 次 CLI invocation。四个 split 各自已发起 1 次真实调用，但没有收到可统计的响应 usage，每轮 `usage.complete=false`、`unknown_requests=1`。其空补丁和 unavailable/incomplete 动作审计属于已发生尝试的失败证据，不从分母删除。全部十个目标都取得独立结论，验收基础设施失败为 0；这不消除模型请求失败。协议错误与其他服务的网络错误发生在相近时间，但当前证据没有确认同一个根因。

知识配对只比较相同 repetition 下的 retrieval-empty 与 retrieval-verified，且只有双方正式独立交付、模型身份/实现 SHA/验收镜像可比时才计算成本差。token 差还要求双方 usage 完整。本批两对均不满足双方正式交付，因此 token、调用和时间差全部为 `null`。失败空组已产生的成本仍保留；不能删掉它们再宣称成功任务节省多少，也不能从读到知识直接推断提效。

后续按[独立补充计划](../experiments/codex-transfer-v1/supplement-plan.json)执行两个 split r3，先保留了一次服务可用性诊断，再使用相同任务与预算启动。baseline r3 15 次调用、247,355/2,644 输入/输出 tokens、185.033 秒；retrieval r3 18 次调用、296,690/2,676 tokens、224.259 秒。两轮均 Submitted 且独立公开检查通过。这是服务恢复后选择的工程补充验证，两组都是空库，不能作知识收益比较；也不替换原四个协议失败，更不能合并成原计划“6/10 成功”。

成本必须按实际阶段分别展示：

| 阶段 | CLI 调用 | 已报告输入/输出 tokens | token 完整性 | 仓库或诊断秒数 |
| --- | ---: | ---: | --- | ---: |
| 源任务 | 15 | 242,373 / 1,996 | 完整 | 181.590 |
| 原十个目标 | 136 | 2,412,570 / 15,737 | 合计 2,428,307 仅为已知下界，另有 4 次未知 usage | 各轮合计 2,356.389 |
| 单独的可用性诊断 | 1 | 8,837 / 16 | 诊断报告值，不是目标任务样本 | 10.621 |
| 补充两个目标 | 33 | 544,045 / 5,320 | 完整，合计 549,365 | 各轮合计 409.292 |

`known_trials=10` 只表示十行都有已知部分的数字，不表示十轮 usage 全部完整；正式汇总另外记录四次 unknown usage 与 `true_total_tokens=null`。调用单位是 CLI invocation，无法观测服务内部的 HTTP 重试。仓库秒数包含主机调度及两轮并发时的争用，不能加总当成整批墙钟，也不是纯模型推理延迟。目标边际开销未包含源任务、镜像准备、独立验收、知识入库、启动失败、CI 和人工排障成本。订阅调用没有可验证的逐次美元计价，金额保持 unknown，不写成 0 元。

**真实运行暴露的问题与修复。** 第一类是 Git 深路径：最初十个任务在准备仓库时读取 loose object 报 `Filename too long`，实际模型调用为 0。托管 Git 明确启用 `core.longpaths=true`，并覆盖本地源传输另起的 upload-pack 进程，不修改用户全局 Git 配置；用隔离仓库验证深路径 checkout、补丁捕获和重新应用。原十个失败的 summary、知识输入、task 导出与 execution 全部保留，之后才按同一任务和预算重试。

归档还暴露普通 Windows 文件枚举静默漏掉十个超长路径 `task.json`。已发布的初版收据仅列 21 个文件，因此增加绑定原收据 SHA 的[清单修正收据](../experiments/codex-transfer-v1/retry-receipt-correction.json)，补列原字节，没有改写旧收据。消费者通过 extended path 实际读取和全量枚举，严格核对联合清单 31 个文件的路径、长度、SHA 和冻结任务内容。“枚举不到”不能当成“文件不存在”。

第二类是 checkpoint 临时路径。四个 split 在发起模型前失败：目标路径 224 字符，加 UUID 临时后缀后达到 261 字符，`CheckpointStore.write` 报 `FileNotFoundError`。保存的同进程诊断复现了问题，它是零模型调用 fixture。修复把私有 trial 与公开 artifact 物理目录缩短为固定名称，逻辑 run ID、题面、模型、预算不变。原四轮 17 件产物先归档，[子集重试脚本](../scripts/retry_codex_transfer.py)生成可发布的预声明收据，启动时再核对原计划、原 execution、全部原件、零调用/零 usage、空库参数与 runner/launcher SHA；已有结果拒绝覆盖。六个已真实执行的 last 没有重跑。

这两次准备阶段共 **14 次零调用启动失败**，与十个实际模型尝试分开记录。它们不增加模型失败样本，也不成为额外目标；确认零 token 只描述模型调用，不表示机器准备与工程排障免费。四个 split 后来实际发过请求的协议失败则必须留在原十个分母，不能再次冒充零调用重试。

第三类是导出时原子替换被拒绝。`split-baseline-empty-r2` 已发生协议失败，随后公开 summary 的 `os.replace` 又遇 `WinError 5`，导出只留下摘要与知识输入。通用 `atomic_json` 现在仅在 Windows 错误码 5/32/33 下，重试同一个已经写好的临时文件替换；延迟为 20、50、100、200、400 毫秒，最多六次尝试。其他错误立即抛出，耗尽保留原目标和临时文件，不重新序列化、不重放业务动作、不请求模型。七项定向测试通过；错误码本身不能证明原因是同步软件、杀毒软件或权限配置。

该轮通过[离线恢复脚本](../scripts/recover_codex_export.py)补全公开产物：验证保存的 result、spec、补丁、request audit 与原摘要、计划和知识输入一致，在全新 staging 调用已有导出函数，前后核对私有文件 hash。原 `summary.json` 与知识输入不改写，新的 `recovered-summary.json` 和[恢复收据](../experiments/codex-transfer-v1/targets/split-baseline-empty-r2/export-recovery.json)绑定原件。原 1 次调用、未知 usage、模型失败、导出错误、耗时、commit 和 dirty 均保留；仅三个原来尚未持久化的 null 请求计数字段由实际 audit 补全。收据 SHA 为 `d3e02407d626ea082e3af027e0235363adcbe83bbba66d0c2ecae954cd52be52`。

第一次恢复的 staging 清单校验发现：私有动作审计不存在时，导出器仍会生成空公共 audit 文件。校验安全失败，原件不变；修正后要求包含这个文件，并在 unavailable 时拒绝非空内容。最终恢复成功、八项相关测试通过，独立 grader 仍判定该模型尝试失败。恢复补齐的是同一尝试的证据，没有新增调用或样本。

源码身份也如实保留。六个 last 的源码为 `5fc6d0d…`、clean=true，package implementation SHA 为 `cfc7b73b…`；原四个 split 重试的 r1 记录 `5fc6d0d…`、r2 记录 `8dff3533…`，均 clean=false，implementation SHA 为 `fdd1291f…`。当时远端发布和本地 Git 元数据同步有时间差，不能为了整齐改写原始 HEAD/dirty；预声明的 runner 字节 SHA 和实际 implementation 身份一起保留。两个补充 r3 为 `ca963a6…`、clean=true，implementation SHA `7470124e…`。全部模型记录相同 CLI 版本与二进制 SHA，但这不能固定远端权重。split 和 r3 不加入 last 的知识配对，也不把路径、调度等实现变化说成知识收益。

**独立报告怎样合并与复核。** 两份下载产物分别保留其历史 request、工作流 checkout 和 ZIP SHA：

| 批次 | Actions run / artifact | verifier checkout | ZIP SHA-256 |
| --- | --- | --- | --- |
| 六个 last | [38030559481](https://github.com/chendi-Shi/repo-delivery-agent/actions/runs/38030559481) / 11660959712 | `8dff3533cc152f00e09cce8192737ae7e10d961f` | `066fbef3dc139372117990292963b1762887927a9a971915c4883ed270556c36` |
| 四个原 split + 两个 r3 | [38032072691](https://github.com/chendi-Shi/repo-delivery-agent/actions/runs/38032072691) / 11663175385 | `620980cbb2f306ec05f66e4693dcd2389aae94bc` | `79c1779a8851c7e48832bd38dde6400e50e328de761e43472a5040b8fb8eeea5` |

[finalization 脚本](../scripts/finalize_codex_transfer.py)要求两个批次分别精确覆盖 6 个预定标签，核对历史 request 原字节、index、manifest、任务、evidence、补丁、模型身份与实际检查。第一批 marker 后来被替换，因此使用各自保存的历史 request，不能用最新 marker 校验旧报告。只有原十个报告被原字节复制到 `verification-merged`，另有一份[合并来源收据](../experiments/codex-transfer-v1/verification-merged/merge-provenance.json)；r3 留在独立 supplement summary，不按时间挑最新成功替换原失败。

安装项目依赖后，在仓库根目录运行这条只读命令即可复核已有产物；不调用模型、不重新执行 Docker 检查、不覆盖输出：

```bash
python scripts/finalize_codex_transfer.py --root experiments/codex-transfer-v1 --check
```

该命令重新计算两份汇总，要求合并目录恰好有十份报告与一份收据，报告与来源逐字节相同，两份 summary 与重算的 canonical JSON 一致；来源、分母或额外文件不匹配时失败。已有输出的 checkout 应使用 `--check`；不带此参数只允许在输出不存在的目录首次生成。聚焦回归测试可单独运行：

```bash
python -m unittest discover -s tests -p test_codex_transfer_finalization.py -v
```

离线复核的信任边界是已保存并固定的下载 provenance 与源文件，它不重新访问 GitHub 鉴权或重新下载 ZIP。当前源码的 CI、测试数、skip 和耗时统一见[验证档案](validation.md)，与历史实验的实际实现身份分别记录。继续跑自己的模型或自定义 OpenAI-compatible endpoint，使用 [README](../README.md) 与 [Codex provider 说明](codex-provider.md)中的新输出目录命令；不要在冻结实验目录覆盖运行。

这份证据支持面试中具体说明：“我基于真实上游实现了仓库交付 Agent 的上下文、Skills、来源可验证的知识库、模型适配、恢复和调度；跑了真实源码任务，并把独立验收、错误和实际成本纳入完整记录。”本案例的 `official_resolved` 仍为 null，公开检查不是官方 SWE-bench 分数；另一个 Requests 单题的官方成功有[独立案例](real-delivery-case.md)，不能挪作本批评分。两次重复、固定小任务和未固定远端权重不支持统计显著或普遍的知识增益，也不支持生产吞吐、物理多机容量、亿级用户或真实企业研发提效的声明。
