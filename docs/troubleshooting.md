# 部署与调试记录（2026-10-16）

## 环境
- Windows 11 + WSL2 + Docker Desktop 29.8.2（数据盘 `D:\DockerDesktopWSL`）
- RTX 2060 6GB / 24GB RAM；Go 1.26.4；Node 24.14；bun 1.2.23（`D:\tools`）；Python 3.11
- Ollama 0.40.5，程序 `D:\Program Files\Ollama`，模型 `D:\Ollama\models`（`OLLAMA_MODELS`）
- 所有构建缓存指向 D 盘：`source dev-env.sh`

## 最终运行拓扑
| 组件 | 运行方式 | 端口 |
|---|---|---|
| postgres（pgvector/pgvector:pg16，库 `new-api` + `kb`）/ redis | 根目录 `docker-compose.yml` | 5432 / 6379 |
| new-api（Go 网关） | 根目录 `docker-compose.yml`，镜像 `agent-stack/new-api:local`（alpine + 宿主机交叉编译的静态二进制，194MB） | 3000 |
| Ollama（qwen3:4b-instruct-2507-q4_K_M, bge-m3） | Windows 原生，GPU | 11434 |

## 问题与修复
1. **Docker Hub 拉镜像失败**
   - 现象 1：`lookup production.cloudfront.docker.com: no such host`（Docker VM 内 DNS 抖动，宿主机解析正常）。
   - 现象 2：重试后 `tls: bad record MAC`，大层下载反复失败；宿主机用 crane 拉 new-api 镜像同样失败，小镜像（redis/postgres/pgvector）重试可成功。
   - 定位：宿主机网络对大文件 TLS 传输偶发损坏（bun 安装也出现 tarball integrity 校验失败），不是 Docker 配置问题。
   - 根因线索：USB 外接无线网卡 COMFAST CF-821AC（Realtek 8811AU/8821AU），驱动 5.1.27.0（2016 年），开启 USB Selective Suspend（5s 空闲挂起）；小文件正常、长时间大流量传输出现 TLS 记录损坏。
   - **根因确认（对照实验）**：同一台机器、同一网络，换用内置 Intel Wi-Fi 6E AX210（驱动 24.20.0.4）后，`docker pull calciumion/new-api:latest`（325MB）首次即成功，耗时 8s；此前 USB 网卡下 docker/crane 共失败 10+ 次。结论：USB 网卡老驱动导致大流量 TLS 记录损坏。
   - 修复：小镜像用 `crane pull` + `docker load` 重试导入；官方 new-api 镜像连续 6 次失败后，改为**自建运行时镜像**：宿主机 `bun run build` → `CGO_ENABLED=0 GOOS=linux go build`（前端经 `go:embed web/dist` 打进二进制）→ `alpine:3.20` 只 COPY 二进制。不依赖 bun/golang 构建镜像，镜像 194MB。附带收益：改 Go 代码后 ~1 分钟交叉编译 + `docker compose up -d --build` 即可验证。
2. **qwen3:4b 思考模式吃掉输出**
   - 现象：OpenAI 兼容接口下 `max_tokens=120` 全部用于 reasoning，`content` 为空；`reasoning_effort:"none"` 让思考文本混入 content；`think:false` 在 `/v1` 接口无效。
   - 修复：本地节点改用非思考版 `qwen3:4b-instruct-2507-q4_K_M`，单次短回答约 1–3s（冷启动 6.6s），显存约 3.1GB。
   - 要点：结构化输出节点（query 生成、reflection）不适合混合思考模型；思考 token 也会计费/占延迟。

3. **本地模型结构化输出不稳定（function calling 返回 None）**
   - 现象：`generate_query` 偶发 `AttributeError: 'NoneType' object has no attribute 'query'`。
   - 根因：`with_structured_output(method="function_calling")` 依赖强制 `tool_choice`，Ollama 不支持强制，Qwen 有时直接输出文本 → 解析结果为 None。
   - 修复：`llm.invoke_structured()` 按模型选方法——本地模型用 `json_schema`（new-api 的 Ollama 适配器把 `response_format` 映射为 Ollama 原生 `format`，即约束解码，见 `relay/channel/ollama/relay-ollama.go:77`），云模型（DeepSeek 不支持 json_schema）用 function calling；解析为空则重试一次后显式报错。
4. **上游 bug：研究循环提前静默结束 / 轮数上限不生效**（gemini-fullstack-langgraph-quickstart）
   - 现象 A：设置 `max_research_loops=1`，reflection 仍跑 2 轮。
   - 现象 B：部分问题 run 状态 "succeeded"，但没有 `finalize_answer`，前端拿不到回答，也无报错。
   - 根因 A：路由函数 `evaluate_research(state: ReflectionState)` 的 state schema 不含 `max_research_loops`，读不到请求参数，回落到默认值 2。
   - 根因 B：reflection 判定 `is_sufficient=False` 却给出空的 `follow_up_queries` 时，路由返回空 `Send` 列表 → 图没有后继节点直接 END。
   - 修复：`ReflectionState` 增加 `max_research_loops`；空 follow-up 视同 sufficient 走 `finalize_answer`。验证：5 个问题 5/5 产出回答，reflection 轮数 = 上限（1/1、2/2）。
   - 要点：LangGraph 条件边函数只能看到其类型标注 schema 中的 channel；`Send([])` 等价于终止分支。
5. **Windows 运行 `langgraph dev` 的两个坑**
   - `.env` 含中文注释 → Python 默认 cp1252 解码崩溃：`dev-env.sh` 设 `PYTHONUTF8=1`。
   - structlog 彩色日志在 Windows 需要 `colorama`：装进 venv。
   - `langgraph dev` 热重载未必生效（traceback 行号对不上当前文件即为旧代码），改动后手动重启更可靠。

6. **本地模型上下文被静默截断**
   - 现象/风险：Ollama 0.40 按显存推导默认上下文，6GB 卡只有 `default_num_ctx=4096`（见 server.log 的 `vram-based default context`）。反思节点的 prompt 塞进多个检索片段后会超长，Ollama 从开头截断，把指令截掉了，但不会报错。
   - 修复：设置用户环境变量 `OLLAMA_CONTEXT_LENGTH=8192` 并重启 Ollama。qwen3 4B 的 KV cache 每 token 约 144KB，8k 上下文约 1.2GB，加上 bge-m3 仍在 6GB 以内。同时反思节点只看每个片段的前 500 字，因为它只需要判断覆盖度，完整片段只交给回答节点。
   - 为什么不按请求传 `num_ctx`：new-api 的 Ollama 适配器只映射 temperature、top_p、num_predict 等参数，不会透传 `num_ctx`。
7. **多轮对话状态累积**（上游 bug）
   - 现象：同一 thread 里问第二个问题时，reflection 读到上一题的片段，而且 `research_loop_count` 接着上一轮计数，一开始就达到上限，不再补检；`follow_up_queries` 带 `operator.add` 归约，第二轮反思时会把第一轮的补充查询再发一遍。
   - 根因：thread 状态会持久化，带归约器的列表 channel 只增不减。
   - 修复：`generate_query` 返回 `Overwrite(...)` 重置 `search_query` / `sources_gathered`，并把轮数归零；`follow_up_queries` 只由单个节点写入，所以去掉归约器。验证：同一 thread 连问两题，第二题的状态只有本题的 1 条查询和 5 个片段。
   - 注意：`Overwrite` 在 updates 流里序列化为 `{"type": "__overwrite__", "value": [...]}`，前端需要兼容这种格式。另外，updates 流只包含 `OverallState` 里的 key，`is_sufficient` 这类字段前端看不到。
8. **知识库入库**
   - 第一次全量入库时进程中途被中断。重跑后只补齐剩下的 35 篇文档（868 个片段，向量化耗时 33s），再跑一次全部 119 篇都是 unchanged，用时 0.5s。依据是按文档内容哈希判断是否变化，每篇文档在一个事务内整篇替换。
   - 网关批量 embedding：一次请求 32 条，new-api 转为 Ollama `/api/embed` 的数组输入。

## 当前可运行链路（2026-10-17 验证）
- 知识库：119 篇文档（new-api 中文文档 78 篇，LangGraph 文档 41 篇），共 1912 个片段，片段长度 p50 为 625 字、p95 为 988 字；单次检索（含向量化）约 0.2–0.5s。
- 检索抽查：相关问题的 top1 相似度在 0.62–0.78 之间，无关问题（"量子计算的发展历史"）约 0.39。阈值设为 0.5，无关问题检索到 0 个片段，回答直接说明知识库未覆盖。
- Agent 端到端（Auto 路由，Medium 档）：单轮检索的问题约 6–8s，触发一轮补检的问题约 15–20s；回答中的引用都能链接到原文。
- 网关冒烟：本地 Qwen 7.1s（含冷启动）/ DeepSeek 1.4s / bge-m3 1024 维向量 4.2s，均经 new-api `/v1`。
- Agent 端到端（2026-10-16，检索节点仍是占位实现；Auto 路由：query/reflection→本地 Qwen，answer→DeepSeek）：11–18s/问，5/5 成功。
- 前端：`http://localhost:5173/app/`（Vite dev，连 `localhost:2024`）。

## 关键命令
```bash
source dev-env.sh
./scripts/build.sh                 # 前端 + Linux 静态二进制
docker compose up -d --build       # postgres(pgvector) + redis + new-api → http://localhost:3000
./scripts/fetch-corpus.sh          # 知识库语料 → runtime/corpus
# 入库（增量）：cd repos/gemini-fullstack-langgraph-quickstart/backend && PYTHONPATH=src .venv/Scripts/python -m agent.kb_ingest --corpus-dir ../../../runtime/corpus
# 调试 Go 代码时也可：docker compose stop new-api && go build -o bin/new-api.exe ./repos/new-api && ./run-new-api.sh
```
