# SQLite 租约时间采样复现

这是无模型的调度边界实验，`llm_calls=0`。旧实现来自提交 `5fc6d0d1ece35cb252e5573998b02e863bda227c`：先读取时钟，再等待 `BEGIN IMMEDIATE` 写锁。等待期间经过的时间没有进入租约判断。

实验使用真实 SQLite 数据库、两个独立连接及一个工作线程；只替换可注入时钟。一个连接持有写锁，另一个连接实际尝试 `BEGIN IMMEDIATE` 后，主线程把时钟从 1000 推进到 1003，再释放写锁。租约长度为 2 秒，不靠长时间睡眠猜测线程进度。

| 边界 | 原实现实际结果 | 内存候选实际结果 |
| --- | --- | --- |
| claim 等待写锁 | 返回已过期的 1002 租约 | 返回从事务取得锁后计算的 1005 租约 |
| heartbeat 等待期间租约过期 | 错误接受，返回 1002 | 拒绝，409 |
| complete 等待期间租约过期 | 错误接受并标记 succeeded | 拒绝，409 |

候选把 register、submit、claim、heartbeat、complete、cancel 六处时钟读取移到取得写锁后。原三项 unittest 全部失败；内存候选三项全部通过，0 错误、0 跳过，0.662 秒。该阶段没有修改生产文件。详见原字节保存的 [复现结果](reproduction.json)、[测试计数](candidate-test-results.json) 和 [候选补丁](candidate.patch)。

复现方法：在临时父目录建立 `.delivery-tools/`，并将原提交检出到同级的 `repo-delivery-agent/`；把 [原复现脚本](reproduction-script.py.txt) 原样复制为 `.delivery-tools/scheduler_lease_clock_review.py`。从父目录运行 `python .delivery-tools/scheduler_lease_clock_review.py`。脚本读取旧源码，校验六个修改点，在内存载入候选，并分别执行上述三个真实锁边界。它会在临时 `.delivery-tools/` 输出新收据；不要覆盖此归档。原 [unittest 源码](regression-test.py.txt) 也按原字节保存，补丁包含该测试文件。可在隔离检出应用补丁后运行：

```text
python -m unittest discover -s tests -p test_scheduler_clock.py -v
python -m unittest discover -s tests -p test_scheduler.py -v
```

之后主代理将补丁应用到主代码，并报告实际执行：新增 3 项测试通过，0 跳过，0.607 秒；调度套件 32 项通过，0 跳过，29.528 秒。这是主代理的工具执行报告，区别于前述内存候选实验；归档步骤没有重复运行测试。归档时读取的主代码按 UTF-8 换行归一化后与候选 SHA-256 一致。原始字节散列、来源与文件清单见 [validation.json](validation.json)。新提交的 CI 结果另行保存。

此复现证明一个独立的事务时间采样缺陷，不能据此断言它就是此前 Windows 心跳测试失败的根因。原提交的 [负载与常规 CI 收据](../scheduler-load-ci/validation.json) 保留不变，其中常规 Windows CI 仍记录 344 项测试、1 项失败。本实验不提供模型任务成功率、跨物理机器表现或生产容量结论。
