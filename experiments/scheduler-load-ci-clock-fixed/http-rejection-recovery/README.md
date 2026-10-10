# 分段请求体导致 401 响应丢失的真实 socket 复现

`1b7dfd9` 的 Windows 普通 CI 在错误 token 的 POST 上出现 `ConnectionAbortedError / WinError 10053`，原测试要求收到 401。此前 CI 失败继续保留；本目录记录随后定位出的一个可控触发路径。[验证收据](validation.json)

复现使用真实本机 TCP 连接、真实协调器与 SQLite，不调用模型。客户端发送 headers，等待服务端作出鉴权拒绝；随后只发送合法 JSON body 的第一个字节，放行服务端写出 401；等最多 30ms 的连接关闭窗口后再发送剩余 body，并读取响应。body 共 32,768 字节，靠合法 JSON 尾部空白填充。测试门控只控制时序，不替换 HTTP 收发、鉴权或数据库行为。

| 同一脚本 | 修复前 | 修复后 |
| --- | --- | --- |
| 实际尝试 | 5 | 5 |
| 完整 401 响应 | 0 | 5 |
| WinError 10053 | 5 | 0 |
| 未授权创建任务 | 0 | 0 |

修复只改变协调器的 401 连接清理：先写出并 flush 拒绝响应；只有无 Transfer-Encoding、唯一且合法的 Content-Length、长度不超过 64 KiB 的请求体，允许最多 250ms 绝对时间的清理。正文只丢弃，不解析、不分发、不复用连接。大长度、歧义长度直接关闭；413 原路径不变。TLS 使用现有 `rfile` 解密后的字节，不绕过 TLS 或读取原始密文。

新增 4 项回归全部通过，0 跳过，0.844 秒：真实 HTTP 分段 body、真实 TLS 分段 body、持续慢 body 的绝对期限、歧义/超限 framing 完全不读取。TLS 使用明确公开的一次性自签测试证书，仅用于回环加密传输测试。原调度 32 项也通过，0 跳过，26.105 秒，包括原鉴权和 oversize 测试，没有放宽断言。

[before.json](before.json)、[after.json](after.json) 与 [原探针源码](probe.py.txt) 均保留原字节，manifest/源码 SHA 见收据。将探针原样复制到仓库同级 `.delivery-tools/probe_scheduler_rejected_body.py`，从父目录运行 `python .delivery-tools/probe_scheduler_rejected_body.py` 可在相应版本复现；运行中不要替换源码。正式回归命令为 `python -m unittest discover -s tests -p test_scheduler_http_rejection.py -v`。

该受控路径能稳定产生与 CI 相同的异常，不能排除其他传输中断原因。新远程 CI 结果需另行验证，不能用本机修复结果改写先前失败收据。
