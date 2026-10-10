# 真实交付案例：Requests 二进制请求体修复

这次实践完成了一条真实链路：公开缺陷输入 → 模型读取和修改仓库 → 显式提交 → 独立 Docker 公开检查 → 官方 SWE-bench harness 验收。第四轮由 `gpt-6-astra` 生成的同一份补丁通过了两条验收通道，官方结果为所选单案例 **resolved 1/1**。此前三轮都没有正式提交，原始失败记录完整保留；这不是完整基准成绩，也不能概括为项目总体成功率 100%。

面试时可以围绕一个核心问题展开：**代码生成只是中间步骤，怎样让一次模型工作在发生异常后仍能形成可核验的交付？** 本案例的工程贡献在模型适配、上下文选择、动作执行、检查点恢复和独立评测。Requests 补丁由真实模型动作生成，没有人工补写任务答案，也没有在失败轮次结束后代为执行未完成的动作。

## 1. 业务问题与交付目标

目标是 Requests 的公开问题 `psf__requests-2931`。用户通过 `requests.put(..., data=u"ööö".encode("utf-8"))` 发送二进制请求体时，2.9 版本出现回归；公开问题记录 2.8.1 可以工作。问题涉及把二进制数据传给 `to_native_string`，可能触发不适合原始字节的字符串转换。冻结仓库基线为 `5f7a3a74aab1625c2bb65f643197ee885e3da576`。[冻结任务](../experiments/codex-cli-proof-v4/model-runs/psf__requests-2931-baseline-codex-cli-proof-v4/task.json)

交付目标包含两部分：原始二进制请求体需要保持内容、类型及长度；原有文本、表单和 URL 参数行为需要兼容。实际应用中的文件上传、非 ASCII 内容或任意字节载荷，都依赖这种边界处理。公开检查使用离线的请求准备过程，无需把测试请求发送到外部服务。

框架把需求拆成可执行环节：冻结任务与基线，向模型提供选中的上下文，通过受控文件工具执行动作，保存响应、用量和状态，捕获补丁，再交给独立环境验收。`files` 后端允许模型读取和修改源文件，不执行仓库测试；测试结果由后续验收产生。源码修改、补丁可应用、显式提交、检查通过和官方 resolved 分别记录，避免混为一个“成功”标志。[提供方及执行边界](codex-provider.md)

## 2. 最终模型实际完成了什么

v4 使用实现提交 `758d1e536b7961f838ac607abb047c041bb0b142`，`baseline` 上下文策略，64,000 字节的 selected-context 预算，最多 24 步。它完成了 **20 次 CLI 调用、约 254.041 秒、350,416 输入 token 和 1,312 输出 token**，最后一步执行显式提交。动作共 20 次：目录枚举 1、搜索 3、文件读取 8、Skill 读取 2、替换 5、提交 1。[运行汇总](../experiments/codex-cli-proof-v4/summary.json) · [逐项核对分析](../experiments/codex-cli-proof-v4/analysis.json)

模型修改了两个文件：

- `requests/models.py`：在 `_encode_params` 中直接保留字符串/字节数据；将 URL 字节参数的字符串转换放到 `prepare_url` 的对应位置，使请求体与 URL 参数的处理分开。
- `test_requests.py`：新增 3 个回归测试函数，展开为 **9 个 case**：PUT/POST × 3 种二进制载荷共 6 个，文本载荷 2 个，已有 query/fragment 的字节 URL 参数 1 个。

这些内容可直接检查 [模型补丁](../experiments/codex-cli-proof-v4/model-runs/psf__requests-2931-baseline-codex-cli-proof-v4/model.patch)。第 10 次调用开始编辑，第 15 次完成最后一次编辑，第 17–19 次读取修改后的源码，第 20 次提交；模型还修正了新增测试中的字面量。源文件复查属于动作证据，并不是执行测试。**9 个 case 是补丁新增的测试内容；独立公开验收实际运行的是冻结的两条检查命令，不能将其说成这 9 个新增 case 已逐一执行。**

这里的调用数是 CLI invocation，不是可精确观测的底层网络请求数。用量来自 20 次完成响应，可能不包含 CLI 内部重试未报告的消耗；美元费用未知。64,000 是选中上下文的字节预算，不是模型 token 上限，CLI 自身提示开销不包含在其中。客户端版本和二进制哈希有记录，远程模型权重没有固定摘要，因此结果不具备完全确定性的复现保证。[运行边界与实测用量](../experiments/codex-cli-proof-v4/analysis.json)

## 3. 三次失败怎样帮助定位问题

| 轮次 | 实际结果 | 能确认的事实与处理 |
| --- | --- | --- |
| [v1](../experiments/codex-cli-proof-v1/analysis.md) | 1 次调用，成功枚举目录后 `PermissionError`；空补丁、未提交 | 没有 traceback，只能把故障范围收窄到首步之后的保存过程或下一步之前。不能指定某个文件操作为根因，也不能归因于 OneDrive。保留已完成响应用量，另建目录重跑收集线索。 |
| [v2](../experiments/codex-cli-proof-v2/analysis.md) | 24 次调用全部只读，反复搜索/读取，最终 `LimitsExceeded`；空补丁、未提交 | `recent` 策略保留最近 3 组上下文，实际出现重复枚举与重叠读取。下一轮改用 baseline/64K 观察行为；这是同时改变策略与预算，不能证明循环由任一因素单独造成。没有复现 v1 异常，也不能据此说问题已修复。 |
| [v3](../experiments/codex-cli-proof-v3/analysis.md) | 11 次响应、10 次已执行动作；已有一处源码修改，仍未提交 | 临时诊断将异常定位到保存第 11 次响应的检查点 `os.replace`，`errno=13`、`winerror=5`。第 11 次响应提出的第二次替换尚未执行；保存下来的部分补丁不等于 Submitted。 |

最近 3 组的配置及选择逻辑见 [上下文实现](../delivery_agent/upstream.py)。诊断采用最小字段：文件 basename、函数、行号、errno/winerror，不记录异常消息、完整路径、locals 或原始模型响应。[v3 安全诊断](../experiments/codex-cli-proof-v3/permission-diagnostics.json)

定位到 `os.replace` 后，仍没有确定哪个进程或文件系统状态导致访问拒绝。v3 的证据也不能倒推出 v1 必然具有同一根因。v2、v3 以及后续 v4 的临时 tracing 用于排查异常，可能增加运行开销；这些耗时属于本次观察，不是严格的性能对照实验。

## 4. 修复框架：只重试同一次检查点替换

检查点保存先构造一个候选状态，将其写入唯一临时文件，执行 flush/fsync，再通过 `os.replace` 更新目标。临时文件包含已经获得的模型响应或已经完成的状态；如果替换失败就重跑整个 Agent 步骤，会有重复调用模型、重复执行编辑或错误累计用量的风险。

因此修复收敛到 [检查点实现](../delivery_agent/checkpoint.py) 中的 `_replace_checkpoint`：

- 只在 Windows 且错误码为 `5/32/33` 时重试同一个临时文件到同一个目标的 replace。
- **最多 6 次 replace 尝试：首次尝试 + 5 次重试**。退避依次为 0.02、0.05、0.1、0.2、0.4 秒，累计配置等待上限 **0.77 秒**；这不是包含系统调用耗时的总时限。
- 重试期间不重新生成临时状态、不重新请求模型、不重放工具动作；替换成功后才推进内存中的检查点状态。
- 非 Windows、未识别错误或次数耗尽时继续抛出异常；失败清理不能覆盖原异常，旧检查点应保持可读。

[检查点测试](../tests/test_checkpoint.py) 覆盖暂时性拒绝后成功、持续失败保留旧状态、未知错误不重试，以及真实进程崩溃后不重放已完成响应和动作。对应 `758d1e5…` 的完整 CI 在 Linux、Windows **各运行 321 项测试，均为 0 skip**，测试耗时分别为 35.747 秒与 120.816 秒；两平台数量不相加为不同测试。它证明框架回归检查通过，真实任务仍需单独验收。[CI 收据](../experiments/ci-checkpoint-rename/validation.json)

v4 的真实运行再次在 `_replace_checkpoint` 捕获到 `errno=13/winerror=5`，异常没有继续传播，之后完成全部 20 次调用并提交。这为修复在本次实际故障上的生效提供了证据。记录没有测量具体退避时长，也没有确定文件占用者，不能声称它等候了完整 0.77 秒，或解决了所有 Windows 权限问题。[v4 安全诊断](../experiments/codex-cli-proof-v4/permission-diagnostics.json) · [恢复核对](../experiments/codex-cli-proof-v4/analysis.json)

## 5. 怎样确认交付闭环

最终补丁 SHA-256 为 `7a9506d7555fdf85c75d1a2d4af5f3ae3d978b9155cf9a34bc3c0e27d5115e28`。模型捕获补丁、正式预测补丁、独立公开验收和官方 harness 记录都绑定到这一份内容。

独立公开验收在冻结基线上应用补丁，使用 Docker 执行冻结检查：一条覆盖文本/表单兼容性，一条覆盖 PUT/POST 的多种二进制载荷及内容长度。报告记录两条命令均 `returncode=0`，该轮 **1 次尝试、1 次正式提交、1 次交付成功、0 次基础设施失败**。验收 artifact 已下载并校验，来源为 run `38023235959`、artifact `11659755110`。[独立报告](../experiments/codex-cli-proof-v4-verification/codex-cli-proof-v4-verification.json) · [artifact 来源](../experiments/codex-cli-proof-v4-verification/artifact-provenance.json)

官方验收也已完成：SWE-bench harness **5.0.2**，提交 `02e7a74ffd0b707aab73d203fe87bdc7c76afc8e`，数据集固定 revision `03e151cf5560b1af6a4363c6a9d766deaaea6b56`。安全报告为 **resolved 1/1、complete=true、基础设施失败 0、歧义失败 0**，submitted/logged patch SHA 一致；官方操作耗时约 72.013 秒，属于后续验收，不包含在前述 254.041 秒本地任务运行耗时中。artifact 已下载并校验，来源为 run `38023235934`、artifact `11659285364`。[官方安全汇总](../experiments/codex-cli-proof-v4-official/summary.json) · [artifact 来源](../experiments/codex-cli-proof-v4-official/artifact-provenance.json)

这两次验收使用保存产物的提交 `d055ba804ed5bbaad65c1b13f317566e9a4e85dc`；模型实际运行的框架版本仍是 `758d1e5…`。原始 summary 中的 null 保留推理结束时尚未验收的状态，后来的验收结果以独立报告追加，避免改写历史。

闭环成立的范围是**这一个冻结 Requests 问题的第四轮尝试**。纵观 v1–v4，是 4 次迭代尝试、3 次未提交、1 次官方成功；不能只展示最终一次的 1/1 来估计总体成功率。这四轮配置和代码有所变化，也不是预注册的同配置独立样本。

## 6. 与岗位 JD 的对应，以及下一步边界

| JD 能力 | 可以用本案例说明的实际工作 |
| --- | --- |
| AI Native 研发工具与基础设施 | 把真实模型接入上游 Agent 协议，建立受控文件动作、状态持久化、正式导出及独立验收链路。 |
| Agent 框架、编排与上下文工程 | 区分请求、响应、动作、保存、提交各阶段；分析 recent/3 组上下文中的重复读取，使用 baseline/64K 继续观察；在保存层处理故障而不重放动作。 |
| 评测与持续调优 | 保留失败分母、未知费用和实际 token；通过审计及最小诊断定位问题，再结合跨平台测试与真实故障复现验证修复。 |
| 系统设计与问题拆解 | 将“模型会改代码”拆解为身份与预算、执行边界、恢复语义、补丁绑定、公开验收及官方结果等可核验模块。 |

本案例尚未证明跨任务知识库带来的收益、并发生产规模下的可靠性或模型总体成功率；也没有隔离上下文策略、预算和框架修复各自的因果贡献。下一步应在预先冻结的多任务集合、对照配置和预算下重复运行，保留所有失败与基础设施未知结果，再讨论成功率、知识复用收益和单位成功交付成本。
