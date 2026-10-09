# 三个真实 SWE-bench Verified 任务

`examples/official-smoke` 来自官方 [SWE-bench/SWE-bench_Verified](https://huggingface.co/datasets/SWE-bench/SWE-bench_Verified/tree/03e151cf5560b1af6a4363c6a9d766deaaea6b56)，固定 revision `03e151cf5560b1af6a4363c6a9d766deaaea6b56`。下载时实际访问 revision API 验证 SHA，再读取该 revision 的 Parquet；2,480,309 字节的文件 SHA-256 与远程 LFS 一致：`bb5b123d29ce70107cc0951cf444894241c570a11d76aec452332c65b01e06d8`。

选择仅依据仓库和公开需求长度，从 requests/flask 候选选择三个 smoke 任务；没有用 gold patch 选“容易成功”的任务。该子集用于验证真实任务链路，不代表 Verified 500 的整体分布。

| 任务 | 仓库 | 公开输入 |
|---|---|---|
| `psf__requests-2931` | psf/requests | `examples/official-smoke/psf__requests-2931.json` |
| `pallets__flask-5014` | pallets/flask | `examples/official-smoke/pallets__flask-5014.json` |
| `psf__requests-1142` | psf/requests | `examples/official-smoke/psf__requests-1142.json` |

公开 task/manifest 只有需求、仓库、固定 commit、任务 ID、版本或执行镜像配置。完整 500 条原始评分数据写入 ignored 的 `data/official-smoke/grading-source.json`；gold patch、test patch、隐藏测试列表和 eval script 没有复制到公开模型输入。`source.json` 保存下载 provenance 和 eval script hash，便于核对，不保存隐藏脚本内容。

重建本地数据需要安装 pyarrow，然后运行 `python scripts/freeze_official_smoke.py`。已有公开快照会先核对 hash 和选中任务，符合固定来源时可重复运行。不要把 `data/` 作为模型工具可访问路径或提交到 Git。

## 固定 harness 与 v5 数据格式

项目固定的 [harness commit `02e7a74`](https://github.com/SWE-bench/SWE-bench/tree/02e7a74ffd0b707aab73d203fe87bdc7c76afc8e) 已采用 v5 评分逻辑。旧形式 `python -m swebench.harness.run_evaluation --dataset_name ...` 仍是兼容 CLI 入口；这并不意味着旧版数据结构自动兼容。该版本的 [make_test_spec](https://github.com/SWE-bench/SWE-bench/blob/02e7a74ffd0b707aab73d203fe87bdc7c76afc8e/swebench/harness/utils.py) 需要官方行中的 `image`、`eval_script`、`log_parser`、`eval_type`、`FAIL_TO_PASS`、`PASS_TO_PASS` 等字段。

下载的原始三条记录已经确认含上述 v5 字段。因此评分时传完整的私有 grading-source，推理时传答案已移除的公开 task。不能把公开 manifest 传给 grader 后期待它凭需求重建隐藏验收。

官方 [ImageSpec](https://github.com/SWE-bench/SWE-bench/blob/02e7a74ffd0b707aab73d203fe87bdc7c76afc8e/swebench/image_builder/image_spec.py) 仍使用 `sweb.eval.x86_64.<instance>` 命名、将 `__` 替换为 `_1776_`，远程 namespace 由镜像指定。本次直接读取官方 `image` 字段，均为 `swebench/sweb.eval.x86_64....:latest`；不额外猜测 registry。

`python scripts/check_official_images.py` 查询 Docker Hub 的公开 OCI manifests，验证返回字节的 digest，解析 linux/amd64 具体 manifest。真实查询收据保存于 `experiments/official-images.json`，三个镜像都存在。这个检查没有下载镜像层，也没有执行评分；不能作为任务 resolved 的证据。

三者压缩层总量各约 1.0–1.15 GB，层之间可能共享。因此仓库较小并不意味着评测镜像很小。正式执行要准备 Linux Docker，并保留 runtime 实际解析的 image ID；镜像 tag 可变，OCI 查询收据与一次具体执行的 image identity 需要一起核对。

v5 默认评分产物在 `logs/evaluation/<run_id>`。将这个具体 run 目录传给 `official-report`。使用不同 run ID 重评不同补丁，避免官方缓存命中上一份结果。代码运行、镜像可下载和官方 resolved 是分别验收的状态；当前任务的实际分数以运行实验报告为准。
