# 验证记录

验证日期：2026-10-10。这里分别记录实现契约、真实容器、真实模型、公开检查和官方评分。CI job 成功表示流程执行成功，不能替代模型交付成功。

## 新增 Docker 交付验证

最新 [5ee0e9a 的 CI](https://github.com/chendi-Shi/repo-delivery-agent/actions/runs/38017726472) 通过 Linux 271 项（32.067 秒）、Windows 271 项（84.719 秒），均无 skip；真实 Docker 与独立 verifier job 也通过。此前 a662b2b 的 Windows 267 项中有一项临时目录短路径别名错误，Linux 同批 267 项通过；该原始失败保留在 [CI 记录](../experiments/ci-closure-cohort-initial/validation.json)。修复统一了根目录与子路径的规范化，并加入真实 Windows 短路径回归。

[21f1f5f 的 CI](https://github.com/chendi-Shi/repo-delivery-agent/actions/runs/38016237676) 实际通过 Linux 230 项（29.331 秒）、Windows 230 项（59.356 秒），均无 skip；Docker 运行与崩溃恢复、既有真实补丁的独立 verifier 同时通过。此前 276c6ce 的 CI 已通过两端各 223 项，Linux 32.632 秒、Windows 68.430 秒。下面的 196 项与 19 次模型尝试属于此前阶段，保留作为历史记录。

第一个 [7B Docker 真实试验](https://github.com/chendi-Shi/repo-delivery-agent/actions/runs/37916573901) 使用固定 Requests commit、原始公开问题和两条公开检查。模型实际请求 24 次，实测输入 46,374、输出 1,107 tokens，usage 完整；仓库运行 636.38 秒。24 条动作均为同一非法 Skill 资源读取，未修改源码，最终 LimitsExceeded。干净 checkout 验收：旧文本/表单兼容检查通过，二进制请求体检查仍报原始 UnicodeDecodeError。未提交，公开交付 0/1，official_resolved 为 null。

完整产物保存在 [closure-proof-v1](../experiments/closure-proof-v1)，GitHub archive SHA256 已核验。原始顶层 summary 漏取 model_calls/usage，保留 null；实际数值来自同目录 hash 绑定的 evidence。新增动作审计让失败能被定位到具体调用；工具指引和真实执行元数据驱动的重复失败反馈已修复并通过协议测试，尚需后续模型结果验证其交付效果。该协议回归是 fixture，不加进模型尝试数。

第二次 [7B Docker 试验](https://github.com/chendi-Shi/repo-delivery-agent/actions/runs/38016237749) 已完成：24 次真实响应，实测输入/输出 53,817 / 732 tokens，用量完整；仓库运行 1,624.684 秒。模型先成功读取一次 Skill，随后 11 次空读取与 12 次非法历史搜索耗尽步数，没有读写仓库源码。空补丁，未提交，两套公开检查均保持一条通过、一条失败，交付 0/1，official_resolved=null。原始顶层 usage 缺失与第一轮属于同一历史导出缺陷，真实数值仍由 evidence 绑定。归档与逐动作原因见 [closure-proof-v2](../experiments/closure-proof-v2/analysis.md)。

固定任务队列第一轮也已执行并完整归档：10 个目标中 8 个发起请求、2 个因来源未成功而在调用前阻塞；没有提交，缺少独立验收，10 个交付结果均保持未知。目标共记录 21 次发起请求，来源另有 1 次；未导出的 Token 用量没有被当作零。加强后的汇总器重新校验原始收据，完整性仍为 false。详见 [全量队列复核](../experiments/closure-cohort-v1/analysis.md)。后续先验证来源成功再启动目标，对历史失败仍保留原记录。

## 此前阶段的实现与运行时验证

本轮源码提交 [a81d8821478f68b476885d48b9847230df29162b](https://github.com/chendi-Shi/repo-delivery-agent/commit/a81d8821478f68b476885d48b9847230df29162b) 的 [CI](https://github.com/chendi-Shi/repo-delivery-agent/actions/runs/37895794373) 已实际全部成功：

| 环境 | 结果 | 用时 |
|---|---|---|
| Windows | 196 项测试通过，0 skip | 74.392 秒 |
| Linux | 196 项测试通过，0 skip | 27.893 秒 |
| Docker job | 真实容器与崩溃恢复集成通过 | 见对应 job |

覆盖上下文选择和完整工具轮次、原始历史回查、Skills 渐进披露、知识来源与 namespace/当前任务隔离、checkpoint 恢复、HTTP coordinator/worker、受限文件操作、真实 upstream 契约、patch 导出与干净应用，以及 predictions/attempts/官方报告绑定。合成模型和合成官方报告验证协议，不构成模型成绩。

此前 [1acbab6 的 CI](https://github.com/chendi-Shi/repo-delivery-agent/actions/runs/37891137623) 已通过 Windows/Linux 各 191 项，分别为 61.935/29.947 秒；更早 [413e653 的 CI](https://github.com/chendi-Shi/repo-delivery-agent/actions/runs/37889587406) 各 189 项，分别为 64.294/31.683 秒。初版 87 项本机回归属于更早的历史验证；本历史阶段以上面 196 项的固定提交记录为准，最新结果见本文开头。mini-swe-agent 固定 2.4.6，真实 DefaultAgent 与官方环境工厂的契约实际执行；缺少依赖而 skip 的测试不能计入通过。

此前真实 Docker 集成和进程崩溃恢复已在 [18e6f2d 对应 CI](https://github.com/chendi-Shi/repo-delivery-agent/actions/runs/37889201826) 通过，下载报告保存在 [docker-validation.json](../experiments/ci-container-v02/docker-validation.json) 与 [docker-recovery.json](../experiments/ci-container-v02/docker-recovery.json)。这两份归档对应此前提交；最新 a81d882 的 Docker job 和独立公开 verifier 也已成功。报告实际核对了持久会话、只读 Git 元数据、关闭网络、新文件 patch、干净应用、独立容器检查，以及进程崩溃后同容器接入、已完成动作不重执行、预算/历史连续和最终清理。

两个 Docker 报告都明确标记 `fixture: true`。恢复报告中的 2 次模型调用和 24/6 tokens 来自可信测试替身，不能加进真实模型 calls/tokens、解题分母或成功率。

本机 Docker Desktop Linux engine 曾不可连接；此限制保留在 [setup-failure.json](../experiments/setup-failure.json)。真实 Docker 证据来自 Linux CI，不把本机探测失败改写成成功。

## 首轮真实编程模型实验

首轮 [真实模型 CI](https://github.com/chendi-Shi/repo-delivery-agent/actions/runs/37889587332) 已执行完成，公开产物保存于 [round-1-1_5b](../experiments/round-1-1_5b)。下载 artifact ID 为 `11598475770`，archive SHA256 为 `356f8b3e74a0604fc3dfd71899e4a78d75ac9088c0e6f02a858ba92fa9d5b165`。

使用真实 Ollama 0.40.2、`qwen2.5-coder:1.5b`（Q4_K_M），模型 digest：

```text
d7372fd828518a4d38b1eb196c673c31a85f2ed302b3d1e406c4c2d1b64a0668
```

运行环境为 2 CPU，provider 为 `ollama_native`，源码推理使用 `files` backend。六次运行使用相同实现 fingerprint `c9f4f4a96bd2ffa8e32e182f0498bf7b50163233da5ce1d17cfb0016277df4fb`。每次预算为 10 steps、12000 context、600 max output、1200 秒 wall-time、180 秒 request timeout、seed 42。预热仅调用空 generate，没有任务 prompt 或固定动作。

[运行清单](../experiments/round-1-1_5b/repository-ablation.json) 与 [独立验证](../experiments/round-1-1_5b/repository-verification.json) 中的 manifest、各 task/evidence/patch hash 已逐一核对相符；官方 predictions 的 sidecar hash 也与实际文件相符。小样本一次尝试不代表模型或策略的总体表现。

### 自建公开需求：count_runs

这个开发任务要求在固定 more-itertools commit 上增加统计相邻连续段数的函数。公开检查覆盖空输入、相邻重复、一次性 iterator 和不可哈希元素。三个 profile 的结果分别统计，不能与三个官方任务混成同一个上下文策略对照。

| Profile | 提交与停止原因 | 确定响应 calls | 实测输入/输出 tokens | 耗时 | 局部 patch / 公开检查 |
|---|---|---:|---:|---:|---|
| baseline | failed；ContextBudgetExceeded | 9 | 13032 / 912* | 84.504 秒 | 436 bytes，可应用；失败 |
| recent | stopped；LimitsExceeded | 10 | 12400 / 1013 | 81.495 秒 | 436 bytes，可应用；失败 |
| retrieval | stopped；LimitsExceeded | 10 | 14956 / 1013 | 129.327 秒 | 472 bytes，可应用；失败 |

\* baseline 的顶层 `usage` 是空对象，因为异常退出未保留该汇总；此处 tokens 是九条真实 response 的 provider usage 逐条求和，不把空对象当成零用量。其余两种策略的顶层 usage 与逐响应之和一致，并标记 complete、unknown_requests=0。

三个运行都没有 `Submitted`。独立 verifier 在第二个干净 checkout 应用 patch 后，使用真实 Docker 执行公开检查，三次均返回 1，报错为不能从 `more_itertools` 导入 `count_runs`。交付结果为 **0/3**；“patch 可应用”没有被算成测试通过。

从实际补丁可定位直接问题：模型新建了根目录 `more_itertools.py`，未修改真实包及导出入口。baseline/recent 的补丁完全相同，并用索引读取 iterable 的前项，不能满足一次性 iterator 契约；retrieval 的函数产出各段长度的 generator，返回契约也不符合“段数”。后两项属于补丁代码审查发现；公开检查在 import 阶段已失败，没有运行到这些行为断言。

上下文记录体现了策略实际执行：baseline 选择消息数从 2 增到 18，没有排除来源；已发送请求的保守输入估算从 3625 增到 9657，下一次请求前触发硬预算。recent 从第 5 次请求开始排除旧来源，最后只选 8 条、排除 12 条，估算最多 5887。retrieval 最后选 18 条、排除 2 条，估算最多 9945，实测累计输入 tokens 高于 recent。UTF-8 估算不是 provider 实测 tokens；这些数据没有证明 retrieval 提高交付成功率。

各 [baseline](../experiments/round-1-1_5b/model-runs/more-itertools__count-runs-dev-baseline-ci-v1/evidence.json)、[recent](../experiments/round-1-1_5b/model-runs/more-itertools__count-runs-dev-recent-ci-v1/evidence.json)、[retrieval](../experiments/round-1-1_5b/model-runs/more-itertools__count-runs-dev-retrieval-ci-v1/evidence.json) 保留真实 usage、请求选择和局部补丁来源。

### 三个官方真实 Issue

三个任务均使用 retrieval，来自固定 Verified revision `03e151cf5560b1af6a4363c6a9d766deaaea6b56`，没有向模型提供 gold 或隐藏测试。

| 任务 | 提交与停止原因 | 确定响应 calls | 实测输入/输出 tokens | 耗时 | 局部 patch |
|---|---|---:|---:|---:|---|
| pallets__flask-5014 | stopped；LimitsExceeded | 10 | 9790 / 200 | 51.961 秒 | 0 bytes |
| psf__requests-1142 | stopped；LimitsExceeded | 10 | 10360 / 263 | 59.847 秒 | 0 bytes |
| psf__requests-2931 | stopped；LimitsExceeded | 10 | 11585 / 252 | 66.082 秒 | 0 bytes |

三题都是 10 次真实响应后到步数限制，没有代码改动和提交。各 usage 标记 complete、unknown_requests=0。公开任务没有 public_checks，因此 verifier 中的 `no_public_checks` 和空 patch 的可应用状态不能当成解题通过。来源排除数均为 0，最后选择 20 条；保守上下文估算最大分别为 7000、7329、8329。

[official-export.json](../experiments/round-1-1_5b/official-export.json) 记录 submitted=0、attempted=3、agent_failure=3、official_grade_performed=false。[predictions](../experiments/round-1-1_5b/official-predictions.jsonl) 实际为 0 bytes；[attempts sidecar](../experiments/round-1-1_5b/official-predictions.jsonl.attempts.json) 保留了三次 stopped 尝试。未调用官方 grader，没有匹配的官方 resolved 报告；这里不宣称“官方 resolved 0%”。

三题的文件没有保存模型动作全文，因此能证实的是步数耗尽、空 patch、无提交，不能仅由这些公开产物断言模型具体在哪一步卡住。完整 evidence 分别见 [Flask](../experiments/round-1-1_5b/model-runs/pallets__flask-5014-retrieval-ci-v1/evidence.json)、[Requests 1142](../experiments/round-1-1_5b/model-runs/psf__requests-1142-retrieval-ci-v1/evidence.json)、[Requests 2931](../experiments/round-1-1_5b/model-runs/psf__requests-2931-retrieval-ci-v1/evidence.json)。

独立 verification 汇总记录六次验证、零基础设施失败。六次是“三个同开发任务策略尝试 + 三个不同官方任务”，其中 `by_profile.retrieval.attempts=4`；比较策略时必须只取 count_runs 三条，避免任务难度混淆。

## 本轮源码与指标修正，第二轮状态

针对首轮错误写入根目录模块的问题，普通 run 在模型调用前新增有界的 Python 源码路径 inventory：只列真实工作区路径，优先项目包与 `__init__.py`，不读取 gold 或预填补丁；路径清单设 1100 字符预算。`repository-context.json` 保存内容和 hash，恢复时与 runtime 绑定核对。文件工具提示明确要求定位现有包与导出入口后编辑。

`delivery-file search` 现在同时接受目录或单文件路径。目录检索最多访问 500 个 Python 文件、返回 40 个匹配，仍检查路径、链接和 Git 元数据边界；不执行模型生成的宿主命令。相关契约已进入最新 CI。路径发现和提示的修改已实现，不等于模型已完成需求。

公开检查指标也已修正：无 public_checks 的任务保留 `delivery_success=null`、`unknown_no_public_checks`，不作为公开检查失败或成功。`attempts` 仍保留全部真实尝试；`public_delivery_denominator` 只包含有公开检查的尝试，并单独报告 `no_public_checks_attempts`。混合或全无检查的输入中，旧兼容字段 `delivery_rate_all_attempts` 保持 null；官方 resolved 仍只来自官方 grader。

首轮历史 JSON 保持原样，其全六次旧汇总 `delivery_rate_all_attempts=0.0` 包含了三题无检查任务，不能当成六题的公开验收成功率。按新口径解释已取得的首轮证据：count_runs 是 3 次有检查、0/3 通过；三个官方任务的公开验收状态未知；模型提交仍是 0/6，官方尝试分母仍为 3。

第二轮 3B、24 steps 的真实模型与独立验证现已完成，完整结果如下。模型、预算和路径提示同时变化，不能将轮次差异归因于某一处修正。

## 第二轮真实编程模型实验

[第二轮 CI run 37891137675](https://github.com/chendi-Shi/repo-delivery-agent/actions/runs/37891137675) 的推理和独立验证流程全部执行成功，对应源码提交 `1acbab6261959d22efde86fa3d1c47ee6023fbf1`。公开产物保存于 [round-2-3b](../experiments/round-2-3b)，下载 artifact ID 为 `11599790991`，archive SHA256 为 `b20f42673649b2b928c5985f770b5088340e9aa74118086caf4cee281ed71c0e`。

实际模型为 Ollama 0.40.2 的 `qwen2.5-coder:3b`（metadata 为 3.1B、Q4_K_M），digest 为 `f72c60cabf6237b07f6e632b2c48d533cef25eda2efbd34bed21c5e9c01e6225`。六次运行使用同一实现 fingerprint `375ee77a5e116e5bf23ddf458cdf0caf10a5ecac8f78cb887716de239e8f77df`、2 CPU、files backend；step budget 改为 24，context/output/wall-time/request timeout/seed 仍分别是 12000/600/1200 秒/180 秒/42。

[运行清单](../experiments/round-2-3b/repository-ablation.json) SHA256 为 `b3b991f73a64fb21b85ae7d46899d7c0654d69d0f0420afbcd90ecf36315fabf`，与 [独立验证](../experiments/round-2-3b/repository-verification.json) 和官方 export 的 source_manifest hash 一致。六组 task/evidence/patch hash 已按实际字节逐一核对；三个官方任务的 repo、commit 和公开需求由 UTF-8 artifact parser 核对，与冻结 manifest 一致。全部六次尝试都保留，没有按结果挑选。

### count_runs 的三种策略

| Profile | 提交与停止原因 | 请求 / 有 usage 响应 | 已知输入/输出 tokens | 耗时 | patch 与公开检查 |
|---|---|---:|---:|---:|---|
| baseline | failed；ContextBudgetExceeded | 9 / 8 | 11589 / 200* | 172.719 秒 | 855 bytes，可应用；SyntaxError |
| recent | stopped；LimitsExceeded | 24 / 23 | 31471 / 1081* | 840.382 秒 | 506500 bytes，可应用；TypeError |
| retrieval | stopped；TimeExceeded | 22 / 21 | 41048 / 933* | 1220.449 秒 | 2566 bytes，可应用；SyntaxError |

\* 三条 audit 都在第 6 次请求记录 FormatError，该调用没有上传可用 usage。baseline 的顶层 usage 仍为空，此处按八条真实 response 求和；recent/retrieval 的顶层 usage 与各自 23/21 条 response 求和相符，标记 `complete=false`、`unknown_requests=0`。零未知远程请求不等于用量完整：这些 tokens 只是已记录用量，不能把格式错误调用的用量当成零，也不能拿它们宣称精确总成本。retrieval 的实际 elapsed 为 1220.449 秒，不能把 1200 秒预算当成实测耗时。

独立 verifier 将三个补丁应用到干净 checkout 后，在真实 Docker 中执行同一公开检查，三次返回码都是 1。baseline/retrieval 的 `more_itertools/__init__.py` 出现 `1:` 等显示行号及未闭合字符串，导入时报 SyntaxError；recent 导入后执行 `next(iterable)`，对 list 报 `TypeError: 'list' object is not an iterator`。公开检查通过数为 **0/3**，Submitted 数也为 **0/3**；`patch_valid=true` 只证明能应用，不代表代码正确。

recent 补丁包含明确的大范围覆盖问题：`more_itertools/more.py` 从 5644 行变为 13 行，`tests/test_more.py` 从 7444 行变为 12 行，`tests/test_recipes.py` 从 1758 行变为 12 行，包入口从 6 行变为 1 行。已有模块能力和测试内容被大量删除，不能把新建的短测试看成完整回归。baseline/retrieval 的补丁还加入了 `from itertools import count_runs`，该导入也不成立；实际公开检查先在包入口语法错误处停止。这里依据实际 patch 和 verifier 报错描述缺陷；原始模型动作没有上传，不能断言使用了哪次 write/replace 或其具体动机。

上下文确实按各自策略执行：baseline 末次请求选入 17 条、排除 0 条，已发送请求的保守估算最大 9051；recent 末次选入 8 条、排除 39 条，最大估算 7102；retrieval 末次选入 14 条、排除 29 条，最大估算 10400。模型调用失败或停止的这些不同路径，不支持“检索提高成功率”的结论。详细 evidence 见 [baseline](../experiments/round-2-3b/model-runs/more-itertools__count-runs-dev-baseline-ci-v2/evidence.json)、[recent](../experiments/round-2-3b/model-runs/more-itertools__count-runs-dev-recent-ci-v2/evidence.json)、[retrieval](../experiments/round-2-3b/model-runs/more-itertools__count-runs-dev-retrieval-ci-v2/evidence.json)。

### 三个官方任务

| 任务 | 提交与停止原因 | 请求 / 有 usage 响应 | 实测输入/输出 tokens | 耗时 | 局部 patch |
|---|---|---:|---:|---:|---|
| pallets__flask-5014 | stopped；LimitsExceeded | 24 / 24 | 39771 / 640 | 571.116 秒 | 25295 bytes，可应用 |
| psf__requests-1142 | stopped；LimitsExceeded | 24 / 24 | 50781 / 487 | 226.418 秒 | 2158 bytes，可应用 |
| psf__requests-2931 | stopped；LimitsExceeded | 24 / 24 | 58612 / 512 | 860.252 秒 | 0 bytes |

三题 usage 均为 `complete=true`、`unknown_requests=0`，仍全部没有 Submitted。Flask 的局部补丁将 `src/flask/blueprints.py` 从 621 行缩为 8 行，Requests 1142 将 `requests/__init__.py` 从 71 行缩为 10 行；这是补丁可直接确认的内容删除问题。官方任务没有公开检查，也未取得官方评分，因此不能把这些可应用补丁记为正确，亦不能从公开产物确定模型动作的具体失败根因。

末次上下文选入/排除数分别为 Flask 26/22、Requests 1142 为 18/30、Requests 2931 为 6/42；已发送请求的保守输入估算最大分别为 10374、10241、10361。详细记录见 [Flask](../experiments/round-2-3b/model-runs/pallets__flask-5014-retrieval-ci-v2/evidence.json)、[Requests 1142](../experiments/round-2-3b/model-runs/psf__requests-1142-retrieval-ci-v2/evidence.json)、[Requests 2931](../experiments/round-2-3b/model-runs/psf__requests-2931-retrieval-ci-v2/evidence.json)。

[官方 export](../experiments/round-2-3b/official-export.json) 实际记录 submitted=0、attempted=3、agent_failure=3、official_grade_performed=false。[predictions](../experiments/round-2-3b/official-predictions.jsonl) 是空文件，其 [attempts sidecar](../experiments/round-2-3b/official-predictions.jsonl.attempts.json) 保留三次 stopped 尝试，hash 与空 predictions 对应。局部 patch 不会绕过提交协议自动进入 predictions。

本轮独立验证为 6/6，基础设施失败为 0；公开检查 eligible 分母为 3，公开交付成功为 0/3，另外三题为 `unknown_no_public_checks`，全尝试公开交付率保持 null。前两轮共保留 12 次真实尝试，其中官方尝试为 6 次，正式提交为 0。后续取得的真实官方 gate 和容器契约结果单独记录于下文，fixture 不计入模型成绩。

第二轮同时改变了模型大小、step budget、源码路径 inventory、目录 search 和提示内容；两轮不能作某个单一策略的因果比较。每个 profile 也只有一次 count_runs 尝试。当前能确认的是运行和证据链路已完成，以及这些具体失败，不是性能提升。

## file-tools-v3 修复后的单案例与第三轮

针对第二轮的行号污染和整文件覆盖，files read 改为返回原始选中源码，范围信息单独记录；write 通过独占创建只允许新文件，已有文件必须精确 replace 或 append。契约测试验证了拒绝覆盖不改变文件。真实模型单案例 [run 37895794679](https://github.com/chendi-Shi/repo-delivery-agent/actions/runs/37895794679) 和后续六案例 [run 37895794485](https://github.com/chendi-Shi/repo-delivery-agent/actions/runs/37895794485) 均已执行并完成独立验证，不因为结果失败而删除。

两个实验使用 a81d882 源码、相同 qwen2.5-coder:3b digest 和实现 fingerprint `a5134a5a4fddb4d045f48cb4c62b4c725a580dcf8559446c9d52d152286712af`。单案例使用 recent、16 steps；第三轮仍为 24 steps、三个 count_runs profiles 和三个官方任务。context/output/wall-time/request timeout/seed 与第二轮相同。下载身份与 archive hash 见 [单案例 provenance](../experiments/file-tools-v3/provenance.json) 和 [第三轮 provenance](../experiments/round-3-file-tools/provenance.json)。

### 单案例

[实际 verifier](../experiments/file-tools-v3/verification.json) 记录 1 次尝试、0 提交、公开交付 0/1、零基础设施失败。模型有 16 次请求及 16 条带 usage 的响应，输入/输出 tokens 为 21800/1216，`complete=true`、`unknown_requests=0`，实际耗时 354.738 秒，停止原因为 LimitsExceeded。

306-byte patch 只向包入口追加 `from more_itertools import count_runs`，没有删除旧内容，但造成从尚未初始化完成的同名包自导入。真实 Docker 公开检查报循环导入 ImportError。可以确认这次未出现行号写入和整文件清空，不能据此声称修复后已完成需求。详见 [evidence](../experiments/file-tools-v3/model-runs/more-itertools__count-runs-dev-recent-file-tools-v3/evidence.json)。

### 第三轮 count_runs

| Profile | 提交与停止原因 | 请求 / 带 usage 响应 | 已知输入/输出 tokens | 耗时 | patch / 公开检查 |
|---|---|---:|---:|---:|---|
| baseline | failed；ContextBudgetExceeded | 10 / 10 | 15018 / 810* | 264.863 秒 | 1403 bytes；循环导入失败 |
| recent | stopped；LimitsExceeded | 24 / 24 | 33240 / 1904 | 550.402 秒 | 306 bytes；循环导入失败 |
| retrieval | stopped；LimitsExceeded | 24 / 24 | 42206 / 1224 | 283.588 秒 | 2704 bytes；循环导入失败 |

\* baseline 顶层 usage 为空，此处是十条实际 response 的 usage 之和，不能写成零；其余两项 usage 完整、unknown_requests=0。baseline 的停止原因是保守 UTF-8 估算在请求前超过 harness 预算，不是 provider 报告的 context overflow。末次选入/排除数分别为 baseline 20/0、recent 8/40、retrieval 22/26；最大保守输入估算分别为 10376、6557、10348。

三个 patch 都能应用，三个公开检查均因同一包入口自导入失败；Submitted 仍为 0/3。baseline/retrieval 增添的函数也仍有 `next(list)` 或错误统计逻辑，但公开检查先在导入阶段停止。第三轮三个 count_runs 和单案例的补丁均只增添内容，没有删除旧模块或测试，源码中未再出现 read 注入的 `1:` 前缀。原始动作没有上传，不能由最终 patch 证明每一次 write 都曾被拒绝；真实效果仍是 0/4 通过。

### 第三轮官方任务

| 任务 | 停止原因 | 请求 / 带 usage 响应 | 实测输入/输出 tokens | 耗时 | patch |
|---|---|---:|---:|---:|---|
| pallets__flask-5014 | LimitsExceeded | 24 / 24 | 44685 / 1683 | 396.108 秒 | 0 bytes |
| psf__requests-1142 | LimitsExceeded | 24 / 24 | 45405 / 2472 | 705.727 秒 | 0 bytes |
| psf__requests-2931 | LimitsExceeded | 24 / 24 | 44094 / 1056 | 198.240 秒 | 0 bytes |

三题 usage 完整、unknown_requests=0；无提交、无公开检查，公开交付保持 unknown。第三轮 [独立验证](../experiments/round-3-file-tools/repository-verification.json) 为 6/6、基础设施失败为 0；公开 eligible 分母为 3、交付成功 0/3。[官方 export](../experiments/round-3-file-tools/official-export.json) 保留 3 次失败 attempts 和空 predictions。第三轮与单案例共七组 task/evidence/patch hash、各 manifest hash 和第三轮官方 sidecar 均已核对，三个官方输入仍匹配冻结 manifest。

三轮六案例加单案例共 **19 次固定 CI 真实尝试：10 次 count_runs、9 次官方任务，0 次 Submitted**。有公开检查的十次尝试全部失败，公开交付为 0/10；九次官方任务没有模型 resolved 成绩。第三轮重复使用 `ci-v2` run ID，因此尝试身份还包含实验目录与 Actions provenance，不能只按字符串 ID 去重。早期独立本机 [环境探索失败](../experiments/model-runs/more-itertools__count-runs-dev-baseline/evidence.json) 也保留：模型 digest/usage 未确定、request audit 为空，未混入这 19 次已执行 CI 推理的对照范围。

工具契约改变、模型/预算改变和不同次数试验都必须分开解释。观察到最后七份 patch 没有大删除或行号污染，不能据此估计缺陷发生率，更不能宣称模型解题成功率提高。

## 真实官方管线：模型 gate 与零模型调用契约

[模型 gate run 37895794518](https://github.com/chendi-Shi/repo-delivery-agent/actions/runs/37895794518) 已真实执行，报告 [summary.json](../experiments/official-gate-v02/summary.json) 为 `no_submissions`：该次导出保留 3 次真实模型尝试，submitted=0、agent_failure=3、unattempted=[]、complete=false，两个 resolved rate 都为 null。没有提交就不运行模型解题评分；这次 gate 的 3 次输入与三轮合计的 9 次官方尝试属于不同统计范围。

另一个 [官方 harness 契约 run 37895794461](https://github.com/chendi-Shi/repo-delivery-agent/actions/runs/37895794461) 实际安装固定 v5.0.2 harness，核对固定 Parquet，在官方 requests-2931 容器中应用人工构造的新增文本文件 patch 并运行官方评估。[契约 summary](../experiments/official-harness-contract/summary.json) 明确 `fixture=true`、`llm_calls=0`、`model_score_eligible=false`，实际 exit=0、耗时 59.012 秒；官方布尔报告为 patch_applied=true、resolved=false、infra_failure=false，contract_passed=true。

这是官方数据、镜像、patch 应用、评估与报告导出的真实功能验证；人工 patch 没有解决 Issue，也不是模型输出。fixture 不进入 19 次真实模型尝试、token 合计或模型 resolved 分数；所有 rate 仍为 null。人工 patch 与报告同目录 patch 的字节/hash 已核对，公开产物只保留布尔字段和 provenance，没有复制 gold、隐藏测试状态或 raw logs。详见 [harness provenance](../experiments/official-harness-contract/provenance.json) 与 [官方 report](../experiments/official-harness-contract/reports/harness-contract-fixture/psf__requests-2931/report.json)。

## 真实 BGE 检索验证与保留的失败

实际本机 bge-m3 调用使用 1024 维向量，模型 digest 为 `7907646426070047a77226ac3e684fbbe8410524f7b4a74d02837e43f2146bab`。语料是三篇公开文档的独立摘要，明确保持 unverified，没有当前 coding task 答案。

[初次探针](../experiments/semantic-search-initial.json) 查询 `one shot traversal`，词项命中 0，iterator 只排第二，top-1 检查失败。这个记录保留。[明确查询探针](../experiments/semantic-search.json) 使用 `Do not scan this collection twice because it gets depleted`，同样词项命中 0，真实语义检索将 iterator 排第一，namespace、跨 namespace 读取拒绝和当前任务排除都通过，用时 53.547 秒。

这验证了真实 embeddings、cosine/RRF 和隔离路径，不能由三篇语料估计总体检索准确率，也不能解释为编程交付提升。

## 历史模型结果与证据边界

三轮和单案例已经实际运行模型、保留 19 次固定 CI 尝试，并完成公开 patch 的干净应用和独立容器检查。结果揭示包路径/导出、源码覆盖、行为契约、上下文预算与步数和时间限制问题；当前没有成功提交或模型官方 resolved 成绩。

196 项双平台契约测试、真实 Docker 恢复、真实 BGE 检索、官方零模型调用 harness fixture 和模型 no-submission gate 均有实际证据。模型交付失败不写成效果修复。后续结果须保存独立目录、模型 digest、实现 fingerprint、所有 attempts 和 matching patch 的真实评分报告，再与既有实验分别报告。当前不宣称上下文策略提高成功率、性能提升百分比或研发提效倍数，也不将本机独立 worker 进程的协议验证写成物理多机生产证明。

复现流程见 [SWE-bench 工作流](swebench.md)、[官方 smoke](official-smoke.md) 与 [实现说明](implementation-walkthrough.md)。
