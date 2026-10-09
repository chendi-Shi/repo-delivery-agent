# 在 CI 运行真实模型实验

`.github/workflows/live-experiments.yml` 运行六个真实本地模型尝试：同一 more-itertools 开发需求的 baseline/recent/retrieval，以及三项冻结的官方公开任务（retrieval）。每个 shard 启动自己的 Ollama，独立拉取模型、冻结 digest，并调用项目真实 native provider 和受限源码编辑 backend。没有测试 provider 或预设工具 actions。

选择的模型为官方 [qwen2.5-coder:1.5b](https://ollama.com/library/qwen2.5-coder:1.5b)，页面列出的 Q4_K_M 文件约 986 MB。它是适合 CPU 小规模链路实验的代码模型，不能把小模型结果外推到更大模型。实际运行的权重身份由 `/api/tags` 获取并保存在证据中，不能仅用可变 tag 作为复现依据。

Ollama 固定为 [v0.40.2](https://github.com/ollama/ollama/releases/tag/v0.40.2)。Linux amd64 `tar.zst` 的 SHA-256 从该版本 [GitHub release API](https://api.github.com/repos/ollama/ollama/releases/tags/v0.40.2) 的 asset digest 核对：

```text
726bee78706c281b0eeef00746efe51a044d71c592c3f0b195820707f31fdf04
```

workflow 下载版本明确的 release 文件，检查 checksum 后仅解压到当前 runner 临时目录。官方 [Linux 手动安装文档](https://docs.ollama.com/linux)说明了相应的打包格式与 `ollama serve` 入口；此 workflow 使用 job 自己的进程，不安装系统服务，不执行浮动的网络安装脚本。

每次尝试预算相同：10 steps、12000 context、600 output、180 秒单请求、1200 秒 Agent wall budget、seed 42。六个 matrix shard 以最多 3 个并发运行，每个 job 最长 60 分钟，避免六个最坏耗时任务挤在同一 60 分钟窗口而遗漏后半尝试。模型已发送的请求不能由 wall budget 撤回，仍受到单请求 timeout 限制。

`ci_live_experiment.py` 仅启动当前 job 的 Ollama session，在结束或异常时清理该进程及其模型子进程。若端口已被别的服务使用，直接失败，不借用或关闭那项服务。Ollama 设置 local-only、loopback 和单请求并行。空 generate 只用于预加载权重，不包含任务提示或固定操作。

collector 下载六个 shard 的公开 artifact，核对任务、补丁 hash 和证据，合并成全新的 `repository-ablation.json`；随后构建官方 predictions 和全部尝试 sidecar，并对有公开检查的任务执行独立 Docker 验收。官方任务没有公开检查时标记 `no_public_checks`，不能把这一项当成官方隐藏测试通过。官方评分由独立 harness workflow 完成。

最终 `live-experiment-evidence` artifact 只包含公开任务、实际 patch、受限证据、已验证的环境身份、验收报告和 predictions。原始 trajectory、完整 provider/tool 输出、模型配置文件、服务日志和宿主绝对路径不上传。每个 shard 的基础设施失败仍会产出失败类型；缺少完整尝试的 collector 会失败，不把缺失记录悄悄删掉来提高成绩。

可在 GitHub Actions 手动触发 `live-model-experiments`。自动触发仅限实验脚本、workflow 和公开 task 样本的改动；导入实验结果本身不会再次触发推理。
