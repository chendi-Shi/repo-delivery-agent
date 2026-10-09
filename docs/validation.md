# 验证记录

验证日期：2026-10-09。此记录区分实现契约、真实运行时与模型任务效果；不提供未经运行的性能结论。

## 已执行验证

最终稳定源码测试：**86 项全部通过，0 error、0 skipped**，用时 131.644 秒。另已完成全部 Python 源文件的语法编译和待提交 Git whitespace 检查。

标准库测试覆盖上下文选择、完整工具轮次、历史 search/read/note、原始观察保存、无答案 task manifest 与 hash 核对、固定 commit checkout、新增/删除/修改文件的 patch 导出和干净重应用、Git 回调禁用、Docker 执行生命周期的故障协议，以及官方 predictions/报告绑定和失败分母。

容器生命周期测试中的 subprocess 是可控测试替身，用于检查超时、清理失败和输出限制的处理；它们不是 Docker 引擎集成验证。伪造模型与报告只用于测试数据契约，不构成模型任务表现或官方评分。

固定 mini-swe-agent 2.4.6 的实际协议测试会实例化真实 DefaultAgent 和真实配置类型，检查工具调用、Submitted 退出和扩展适配。缺少此依赖时对应测试明确 skip，不能把 skip 写成通过。固定上游源码来源与包版本记录在代码和 README 中。

本轮 5 项实际上游契约均已执行：3 项 DefaultAgent、1 项 Submitted、1 项真实 SWE-bench 环境工厂的镜像派生与受控类动态加载。测试读取完整官方 wheel 的真实 metadata，没有修改成熟上游或替换其 metadata。为绕过缓慢的完整依赖下载，测试进程使用经过哈希核对的完整 wheel 和现有支持依赖；这不等于完整 `.[agent]` 环境安装完成。

| 核对对象 | 精确记录 |
|---|---|
| mini-swe-agent wheel SHA256 | `a35463c553ac825c7773b03cfa69cd44958e3af20155dcc5711fdf9e4c67cd54` |
| DefaultAgent 源码 Git blob | `ca310e37d7c61888c576f73f5e1790ac769407b1`，与固定上游 commit 一致 |
| PyYAML 完整官方 wheel SHA256 | `5fcd34e47f6e0b794d17de1b4ff496c00986e1c83f7ab2fb8fcfe9616ff7477b`，与官方 PyPI JSON 一致 |

## 本机环境限制

本机 Docker CLI 已安装。最初的服务检查返回过服务器版本，之后正式容器验证中 `docker info` 超时，后续服务探测报告 Docker Desktop Linux engine 管道不可连接。因此尚未完成真实 Docker 集成验证，也没有运行成功的真实模型仓库任务。

已配置 [GitHub Actions](https://github.com/chendi-Shi/repo-delivery-agent/actions)：Linux/Windows 安装完整 upstream extras 后运行测试；另一个 Linux job 实际运行 Docker 集成脚本并上传验证 JSON。配置存在不等于检查已经通过，实际状态以对应源码提交的 Actions 记录为准。

完整 `.[agent]` 安装下载较慢；依赖安装状态与真实上游契约测试单独记录，不能将安装完成等同于模型任务完成。

提供的 `scripts/docker_smoke.py` 是可信代码驱动的集成检查：在真实容器里修改仓库，确认跨命令文件状态、只读 Git 元数据、禁止网络、实际 patch 和第二个 checkout 验证。它不调用模型，即便运行成功也不是模型成功率。

```bash
docker pull python:3.12-slim
python scripts/docker_smoke.py --output runs/docker-validation.json
```

请先核对本脚本 `--help` 与本地 Docker 状态，再运行真实模型示例。镜像、模型与仓库依赖需提前准备。

## 尚未取得的结果

- 没有官方 SWE-bench resolved 结果或成功率。
- 没有 baseline/recent/retrieval 的模型效果对照结论。
- 没有“性能提升百分比”或“研发提效倍数”的实测证据。

真实模型任务、公开检查和官方评分的完整复现实验见 [SWE-bench 工作流](swebench.md)。实验设计与岗位对应见 [设计说明](design.md)。
