# Codex CLI 模型提供方

`codex_cli` 把本机已登录的 Codex CLI 接到现有 mini-swe-agent 执行循环。每次调用只生成一个 `{"command": "..."}` 响应；适配器把它转换成上游 `bash` action，再由项目的执行环境处理。CLI 本身不负责修改目标仓库。本文说明实现和验收条件，不声明真实仓库任务已经成功。

官方文档提供了 `codex exec`、JSONL 事件、`turn.completed.usage` 与 `--output-schema` 的接口说明。本项目在这些接口之上增加临时目录、输出边界、事件校验和测量字段。[非交互模式](https://learn.chatgpt.com/docs/non-interactive-mode)

## 本机登录与启动

先在本机安装 Codex CLI，并通过其正常登录流程登录 ChatGPT。`codex login status` 用于检查登录状态；未登录时运行 `codex login` 完成浏览器登录。本项目不会读取或复制认证文件、API key，也不会把本机登录状态上传到 GitHub Actions。[CLI 命令参考](https://learn.chatgpt.com/docs/cli/reference)

Windows 使用原生 `codex.exe` 的实际完整路径，不能把 `.cmd` / `.bat` 包装器当作原生可执行文件。下面是 PowerShell 示例，第一行需要替换为自己的安装位置：

```powershell
$codexBinary = 'C:\tools\codex\codex.exe'
& $codexBinary --version
& $codexBinary login status
```

在仓库根目录安装项目及固定的上游依赖后，单次源代码试验可使用：

```powershell
python -m pip install -e ".[agent]"
python scripts/run_codex_trial.py `
  --task examples/delivery-suite-v1/requests-2931-public.json `
  --model gpt-6-astra `
  --codex-binary "$codexBinary" `
  --output runs/codex-cli-proof-v1 `
  --evidence experiments/codex-cli-proof-v1 `
  --label v1 `
  --profile recent `
  --max-steps 24 `
  --context-tokens 32000 `
  --wall-seconds 1800 `
  --request-timeout 120
```

这只是复现命令，不是已完成结果。每次试验使用全新的输出和证据目录；模型名称必须是该账号实际可用的显式名称。`gpt-6-astra` 是本次配置的远程别名，不是所有账号均可用的承诺。

`--profile` 接受 `baseline`、`recent` 和 `retrieval`，默认 `recent`。`baseline` 保留历史，在超预算时明确停止；`recent` 和 `retrieval` 选择历史视图。切换策略或预算属于新的试验配置，必须使用新目录并记录变化，不能覆盖先前失败或直接推断策略收益。

`runs/` 被 Git 忽略，用来保存工作副本、完整轨迹、请求视图和检查点。`experiments/` 只接收经过筛选的任务、补丁、动作审计、用量和配置证据。发布前仍须检查导出内容；忽略目录不是权限隔离机制。推理使用本机认证，独立 CI 验收只需要保存的补丁和公开检查，不需要模型凭据。

需要自行组合运行器时，也可使用 `python -m delivery_agent.cli run`，指定 `--provider codex_cli --backend files --model gpt-6-astra --codex-binary <原生可执行文件路径>`，以及必填的 `--task`、`--output`。这个通用入口保存原始结果；上面的试验脚本另外准备独立验收和正式评测的导出材料。

## Python 接口与进程参数

实现位于 [`delivery_agent/codex_model.py`](../delivery_agent/codex_model.py)，接口与项目其他 Model 实现一致：

```python
from delivery_agent.codex_model import CodexModel

model = CodexModel(
    model_name="gpt-6-astra",
    model_kwargs={
        "binary": r"C:\tools\codex\codex.exe",
        "timeout": 120,
        "reasoning_effort": "low",
    },
)
identity = model.resolve_identity()
response = model.query(selected_messages)
```

`model_name` 必填。`binary` 可以省略，此时适配器查找 PATH；显式指定时使用真实文件路径。`timeout` 为有限正数，最大 3600 秒。推理强度接受 `low`、`medium`、`high`、`xhigh`，实际模型是否支持仍由 CLI 判定。其他 provider 选项会被拒绝，特别是 `seed`、`temperature`、`max_tokens`、`num_ctx`。`format_message`、`format_observation_messages`、`get_template_vars` 和 `serialize` 负责上游动作、观察与配置协议。

每次 `query` 新建空临时目录，只在其中写入命令 JSON Schema。选中的消息通过 stdin 传入；保留角色、文本和上游工具调用关联字段，排除消息的内部 `extra` 元数据。代码使用参数数组启动子进程，不经过宿主 shell。关键参数为：

| 参数或配置 | 当前实现的用途 |
| --- | --- |
| `--ask-for-approval never`、`--sandbox read-only` | 无交互审批；CLI 采用只读沙箱 |
| `exec --ignore-user-config --ephemeral` | 独立调用，跳过用户配置，不建立供下次查询续用的会话 |
| `--skip-git-repo-check` | 允许在空临时目录启动 |
| `--json --output-schema <临时 schema> -` | JSONL 事件和单个命令对象；prompt 由 stdin 输入 |
| `--model <显式模型名称>` | 不依赖用户配置中的默认模型 |
| `model_provider="openai"`、`forced_login_method="chatgpt"` | 使用内置 OpenAI 提供方及本机 ChatGPT 登录 |
| `web_search="disabled"`、`mcp_servers={}` | 关闭网页搜索和本次调用的 MCP 配置 |
| `project_doc_max_bytes=0`、启用 `skip_host_skill_discovery` | 禁止附加项目说明和宿主 Skill 发现 |
| `model_reasoning_effort="low"` | 示例运行采用的推理强度，可通过接口调整 |
| 禁用原生功能开关 | 详见下面的执行边界；以 `_argv()` 为完整参数来源 |

Schema 仅允许一个非空字符串字段 `command`。完成响应随后转换为一个上游 `bash` 工具调用；`files` 环境会再检查命令是否属于允许的文件协议。

## 身份、预算与测量

`resolve_identity()` 读取实际 `codex --version` 输出并计算本机可执行文件的 SHA-256。每次查询前重查文件哈希，运行中二进制变化会中止。记录包含 `kind=codex_cli`、显式 `model`、`cli_version`、`binary_sha256`、`reasoning_effort`、`weight_digest_available=false`。本机接入工作使用过 `codex-cli 0.154.0`；适配器并没有把所有其他版本自动认证为兼容。

二进制 SHA 只能说明客户端来源。远程模型名称可能指向服务端更新后的模型，因此 `model_digest` 保持 `null`，独立验收标为 `remote_alias_not_weight_pinned`。不能把客户端哈希称为模型权重哈希，也不能把不同日期的远程别名调用视为权重固定的配对实验。

项目的 `--context-tokens` 历史名称对应保守的 selected-message 预算：计算选中消息的 UTF-8 JSON 字节数，再给每条消息增加 framing 余量；它不是模型专用 tokenizer 的计数。外层会扣除 reserve，保留系统说明、原始任务及最新完整动作循环，其余历史按 profile 选择。

适配器随后添加的响应协议说明、JSON 包装、Schema，以及 CLI 自己的系统说明和其他原生开销，不在这个 selected-message 预算内。证据明确记录 `native_prompt_overhead_outside_selected_context_budget=true`。因此不能把 `--context-tokens 32000` 称为“整次 Codex 请求最多 32000 个真实 token”。

| 测量字段 | 可支持的解释 |
| --- | --- |
| `turn.completed.usage.input_tokens` / `output_tokens` | CLI 对已完成 turn 报告的实测用量；映射到项目输入/输出 token 字段 |
| `cached_input_tokens`、`reasoning_output_tokens` 等 | 按 CLI 原始字段保留；不会再次加到主计数造成重复统计 |
| `model_calls`，单位 `cli_invocations` | 外层记录的适配器调用尝试；不是精确的底层 HTTP 请求数 |
| `underlying_transport_request_count=null` | CLI 内部传输次数没有独立可验证的计数 |
| `usage.complete` | 只对项目记录的调用是否都有完成用量成立；不覆盖可能未报告的内部重试用量 |
| `cost_source=chatgpt_plan_unpriced`、`cost_unknown=true` | 无可验证的每请求美元价格；费用总额保持未知 |
| 内部 `extra.cost=0` | 满足上游数值记账接口的占位值；对外 `reported_cost=null`，不表示免费 |

适配器不会自动重启失败的 CLI 调用，并禁用 `unbounded_connection_retries`。但本次 CLI 拒绝覆盖保留的内置 `openai` provider，不能通过 `model_providers.openai.*` 把其 HTTP/SSE 重试参数设为零。实现保留内置认证方式，明确记录 `internal_transport_retries_controlled=false`；不另建自定义凭据提供方绕过限制。失败或缺失响应的用量保持未知，不能补零。

这个接入没有 provider sampling seed，也没有已实施的生成 token 上限。通用 CLI 的 `--seed` 不会传入 Codex；`--max-output-tokens` 只影响外层 reserve，不能据此声明 Codex 停止生成的 token 数。未知价格下 `--cost-limit` 也不能充当可靠美元支出上限。实际约束来自外层步数/运行时间检查、每次 CLI 超时，以及 stdin/stdout 各 8 MiB、stderr 1 MiB 的字节上限。

## 原生工具禁用与拒绝策略

当前禁用 `shell_tool`、`apps`、`plugins`、`remote_plugin`、`multi_agent`、`hooks`、`code_mode_host`、`browser_use`、`computer_use`、`image_generation`、`view_image`、`goals`、`memories`、`skill_search`、`workspace_dependencies`。目标仓库不会成为 CLI 的 cwd；适配器也不向临时目录复制仓库或参考答案。任务源代码只能通过外层执行环境返回的选中消息进入 prompt。

JSONL 消费端要求一个合法的 thread/turn 顺序、恰好一个完成的 `agent_message`，以及包含有效非负整数用量的 `turn.completed`。允许无执行副作用的 reasoning 事件；只放行已识别、发生在 turn 开始前的两个“宿主功能禁用”启动诊断。其他错误、未知事件、多个回答、缺失用量或未完成 turn 都会中止。

`command_execution`、`mcp_tool_call`、`web_search`、`file_change`、计划工具及未知 item 在 started/updated/completed 阶段均被拒绝。即使工具标记为失败，也不把这次调用接受为合规响应。stdout/stderr 并行限量读取，超时或协议违规时终止本次拥有的子进程组，随后清理临时目录；stderr 原文不进入公开异常，只保留必要的计数和哈希。

这些措施依赖当前 CLI 的功能开关与事件协议。事件拒绝可以发现偏离，不能撤销已经发生的宿主操作；只读沙箱和空 cwd 也不等于为任意未来 CLI 提供完整 OS 隔离。新 CLI 版本需要重新检查开关和真实探针，不能仅靠“最终输出是 JSON”断言工具没有运行。

## 提交、独立 Docker 验收与正式评测

`files` 后端仅解释受限的文件操作协议，不运行模型生成的宿主命令，也不执行仓库测试。它可以读取和修改允许的源文件、保留工作区变化，并在干净 checkout 上验证补丁能否应用；此时测试状态仍为 `deferred`。模型说“测试通过”没有验收效力。

Agent 必须实际执行 `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT` 提交动作，上游结束状态为 `Submitted`，且运行结果 `status=submitted`，才构成正式提交。超时、步骤耗尽、上下文超限以及异常退出都保留为失败或停止；即使导出了部分补丁，也不能升级成 submitted。

公开检查交付要求：证据和补丁哈希对应、模型身份信息有效、在冻结的仓库 commit 上重新应用补丁、由独立 Docker 环境执行全部冻结公开检查，并且运行已正式提交。验收命令为：

```powershell
python scripts/grade_experiments.py `
  --manifest experiments/codex-cli-proof-v1/repository-ablation.json `
  --output runs/codex-public-verification.json `
  --pull-images
```

[`codex-delivery-verification.yml`](../.github/workflows/codex-delivery-verification.yml) 在独立 GitHub Actions runner 上执行同一流程。报告保存实际检查退出码、输出哈希、补丁/任务/证据哈希和解析后的镜像身份。没有公开检查或验收基础设施失败时，结果保持未知；失败尝试保留在对应分母中。验收脚本正常退出或 workflow 显示绿色，只说明流程完成，仍须读取报告中的逐项结果。

对于 `psf__requests-2931`，试验脚本还核对原始公开 issue 的实例 ID、仓库、base commit 和问题描述没有变化，再生成正式 predictions。导出只接受真实 provider、`status=submitted` 且 `exit_status=Submitted` 的结果，并保留未提交尝试记录；本地公开检查和 patch-valid 结果不会被用来挑选正式提交。正式 `resolved` 只能由对应补丁的 SWE-bench harness 报告确认，公开检查通过与 predictions 文件存在均不能代替它。

当前文档所列接口测试使用独立、确定性的子进程 fixture，覆盖输出上限、超时、工具事件拒绝、上游动作/观察和文件修改提交协议。它们证明接口契约，不证明真实模型的仓库修复能力。真实任务结论应另附具体 attempt、原始失败记录、独立验收结果和正式评测结果；单次样本也不能支持稳定提效或跨模型优劣结论。
