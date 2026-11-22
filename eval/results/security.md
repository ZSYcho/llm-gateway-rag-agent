# 访问控制检查

| 检查 | 结果 | 详情 |
|---|---|---|
| 未携带凭证被拒绝 | PASS | HTTP 401 |
| 未知 Agent 密钥被拒绝 | PASS | HTTP 401 |
| 客户端自造的匿名会话被拒绝 | PASS | HTTP 401 |
| 匿名访客读不到其他匿名访客的线程 | PASS | HTTP 404 |
| 匿名访客搜索不到其他匿名访客的线程 | PASS | HTTP 200，结果含对方线程=False |
| 匿名访客不能在其他匿名访客的线程上运行 | PASS | HTTP 404 |
| 匿名访客能读自己的线程（对照） | PASS | HTTP 200 |
| 请求体伪造身份无效 | PASS | 检索分组 ['public']，回答含口令=False |
| 普通配置项伪造分组无效 | PASS | 检索分组 ['public'] |
| ops 身份可读内部文档（对照） | PASS | 检索分组 ['ops', 'public']，回答含口令=True |
| 匿名访客读不到 ops 的线程 | PASS | HTTP 404 |
| 匿名访客搜索不到 ops 的线程 | PASS | HTTP 200，结果含 ops 线程=False |
| 匿名访客不能在 ops 的线程上运行 | PASS | HTTP 404 |
| 受限令牌调用白名单外模型被拒绝 | PASS | HTTP 403 {'error': {'code': '', 'message': 'This token has no access to model deepseek-fl |
| 受限令牌调用白名单内模型成功（对照） | PASS | HTTP 200 |
| 令牌 A 查不到令牌 B 的运行成本 | PASS | HTTP 200，可见调用数=0 |
| 令牌 B 能查到自己的运行成本（对照） | PASS | HTTP 200，可见调用数=1 |
| 成本接口未带令牌被拒绝 | PASS | HTTP 401 |
