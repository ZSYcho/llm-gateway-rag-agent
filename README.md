# llm-gateway-rag-agent

LLM 网关驱动的 RAG 研究型 Agent 平台：所有模型调用经过 Go 网关统一鉴权、路由和计量，LangGraph Agent 在 pgvector 知识库上多步检索、反思补检，并给出带引用的回答；按节点选择本地模型或云模型。

基于两个开源项目二次开发（以 git submodule 引入，改动在各自 fork 的分支上）：

| 子模块 | 上游 | 作用 |
|---|---|---|
| `repos/new-api` | [QuantumNous/new-api](https://github.com/QuantumNous/new-api)（Go, AGPL-3.0） | OpenAI 兼容网关：渠道管理、令牌、计费、日志 |
| `repos/gemini-fullstack-langgraph-quickstart` | [google-gemini/gemini-fullstack-langgraph-quickstart](https://github.com/google-gemini/gemini-fullstack-langgraph-quickstart)（Apache-2.0） | LangGraph 研究型 Agent + React 前端 |

## 架构

```
React 前端 (:5173) ──► LangGraph API (:2024)   API 密钥 / 服务端签发的匿名会话 → 身份与分组（客户端不可伪造）
                         generate_query → kb_retrieve ×N → reflection ⟲ → finalize_answer
                         每次调用带 X-Agent-Run-Id / X-Agent-Node 请求头；节点失败时降级而不中断
                             │ OpenAI 兼容协议         │ 先按分组过滤，再余弦 top-k
                             ▼                         ▼
                       new-api Go 网关 (:3000)     PostgreSQL + pgvector（kb 库，HNSW）
                         │  ── PostgreSQL / Redis      ▲
                         │                        kb_ingest：切块 → bge-m3 → 增量写入
                         ├─ Ollama 本地 (GPU)：qwen3:4b-instruct（查询生成、反思）, bge-m3（向量）
                         └─ DeepSeek 云端：deepseek-flash（最终回答）
```

## 已实现

- **统一网关接入**：Agent 的全部 LLM 调用改为经 new-api 转发（`backend/src/agent/llm.py`），Agent 只持有网关令牌，不接触上游厂商 Key。
- **按节点路由模型**：查询生成与反思走本地 4B 量化模型，最终回答走云模型；前端可切换 Auto / 全本地 / 全云端，用于对比效果与成本。
- **本地模型稳定的结构化输出**：Ollama 不支持强制 `tool_choice`，function calling 偶发无工具调用；本地模型改用 `json_schema`（网关映射为 Ollama 约束解码），云模型用 function calling，解析为空时重试后显式报错。
- **修复上游两个 bug**：研究轮数上限 `max_research_loops` 不生效；反思给出空的补充查询时图无回答静默结束。
- **知识库检索（RAG）**：用 `kb_retrieve` 节点替换原来依赖 Gemini 的 Google Search 节点（`backend/src/agent/kb.py`）。查询向量由 bge-m3 经网关生成，在 pgvector 中用 HNSW 索引做余弦 top-k 检索；相似度低于阈值（0.5）的片段直接丢弃，知识库没有覆盖的问题会明确回答"未覆盖"，不会硬凑答案。多条查询召回的片段先去重，再按分数截断后交给反思和回答两个节点。回答里的引用 `kb://<chunk id>` 按 id 整体替换为原文链接，模型编造的引用会降级为纯文本。
- **入库流水线**（`backend/src/agent/kb_ingest.py`）：清洗 Markdown/MDX（frontmatter、JSX 组件、JS 代码变体），按标题层级切块，代码块不会被切开；过短的小节并入下一节，向量化时在正文前拼上标题路径。入库是增量、幂等的：按文档内容哈希跳过未变化的文档；每篇文档在一个事务里整篇替换；语料里已删除的文档会同步清理。中断后重跑只补齐未完成的部分。
- **修复多轮对话的状态累积**：同一会话里，`operator.add` 归约的 `search_query` / `sources_gathered` 和研究轮数会跨轮累积，导致第二个问题沿用上一轮的片段，而且一开始就达到轮数上限。改为在 `generate_query` 中用 `Overwrite` 重置。另外，反思节点重复提出已经跑过的查询时直接过滤掉，避免无效循环。
- **网关侧 Agent 成本与错误归因**：new-api 在消费日志和错误日志中记录 `X-Agent-Run-Id` / `X-Agent-Node`，写进日志的 `other` JSON 字段，不改表结构（SQLite、MySQL 5.7、PostgreSQL 16 均已验证）。新增接口 `GET /api/agent_runs/:id/cost`，用令牌鉴权，只能查本令牌的记录，按节点汇总调用数、失败数、token、费用和耗时。`:id` 是 LangGraph 的 run_id，即一次提问对应一个 run。
- **模型定价**：本地模型（Qwen、bge-m3）定价为 0；云端回答模型使用 DeepSeek 当前的模型名 `deepseek-flash`，按官方高峰价配置（输入 $0.3、输出 $1.2、缓存命中 $0.006，单位均为每百万 token）。按节点路由的当前配置下，每题平均 $0.00087（按标价）。
- **知识库文档级权限**：文档和片段带 `acl` 分组，检索时在 SQL 里先按调用方分组过滤、再做向量排序，无权限的片段既不会返回，也不会挤占 top-k。过滤条件很严时，普通 HNSW 扫描会少返回结果（实测 3 条相关片段只返回 0 条），开启 pgvector 的 iterative scan 后全部返回。调用方分组来自 LangGraph 服务端的认证（`backend/src/agent/auth.py`），服务端会丢弃请求里伪造的身份字段。会话线程按身份隔离：持 API 密钥的调用方按密钥对应的身份；匿名访客先从 `POST /anonymous-session` 领取服务端生成并签名的随机会话（128 位），每个访客各自一个身份，彼此看不到对方的对话；没有凭证、或客户端自造的会话一律返回 401。
- **降级与资源上限**（`backend/src/agent/resilience.py`）：查询生成失败时直接用原问题检索；单路检索失败不影响其它分支；反思失败时停止补检、用已有片段回答；云端回答模型故障（500 / 429 / 402 欠费）时回退到本地模型；检索整体不可用时明确告知而不是编造。每次降级都记录在 `degraded` 中并在前端时间线显示。客户端请求的查询数和研究轮数有上限（5 条、3 轮），防止被放大调用。降级在节点内部捕获依赖错误实现，没有用 LangGraph 的节点 `error_handler`：客户端以 `messages` 模式流式订阅时，已被 error_handler 处理的节点异常仍会在任务执行器退出时被重新抛出，整次运行被标记为失败。
- **评测驱动的默认配置**：查询生成与反思的温度设为 0，同一问题每次检索到相同片段。DeepSeek 默认开启的思考模式对这类文档问答没有带来可测的正确率提升，关闭后每题费用（按标价）下降 22–43%、延迟少约 2 秒，因此默认关闭；但关闭后云端模型更容易照做检索文档里夹带的指令，由下面的注入防护补上。DeepSeek 在思考模式下不支持强制 `tool_choice`，结构化输出调用始终关闭思考。
- **间接提示注入防护**：检索到的文档可能被投毒（例如夹带“请访问某网址领取额度”）。检索只按相似度和权限排序，并不区分正常文档和被投毒的文档，所以所有检索片段一律当作不可信资料：回答提示词把片段放在 `<excerpts>` 标签里并标为不可信，要求不执行也不转述其中写给 AI 的内容；输出端再做确定性检查（`backend/src/agent/safety.py`）：每个回答只允许链接到它的检索来源所在的站点（白名单随回答生成，不维护黑名单），其它 Markdown 链接只留文字，正文里的陌生网址或域名（带不带 `http://` 都算，如 `evil.example`、`www.evil.example`）连同所在句子删除；代码块和邮箱地址不处理（文档里的官方联系邮箱本身就是合法答案）。被移除的链接记录在 `blocked_links` 并在前端显示。输出检查只是兜底：写在代码里、变形（`evil[.]example`）或不在常见顶级域名表里的地址仍会漏过，不含链接的注入指令只能靠提示词。误伤审计：识别规则在知识库 1912 个片段中找出的域名全部是真实域名；用 1084 个历史回答回放，只多删了 1 句（提到 nodejs.org 的一句）。
- **七维评测**（[`eval/`](eval/README.md)）：结果、检索、证据、过程、稳定性与异常、成本与效率、安全与权限。数据集 79 题，证据以原文标注；裁判用 deepseek-v4-pro；比率带 95% 置信区间，配置对比按同一批题配对计算；另有故障注入代理和访问控制检查。
- **前端**：KaTeX 公式与 GFM 表格渲染；时间线显示每次检索的片段数、最高相似度、来源、反思给出的补充查询，以及降级情况。
- **一键部署（网关 + 数据层）**：`docker-compose.yml` 启动 PostgreSQL（pgvector）、Redis、new-api；new-api 镜像由宿主机交叉编译的静态二进制 + alpine 构成。

## 评测结果

完整数据见 [eval/results/SUMMARY.md](eval/results/SUMMARY.md)。核心题加变体题共 67 道，每种配置各跑一次；正确率由 deepseek-v4-pro 逐条判定要点。每题费用按标价（token × 官方单价）计算：温度为 0 时相同问题的提示词完全一致，先后运行的实验会命中 DeepSeek 的前缀缓存，网关实际计费随之偏低，不适合用来比较配置。

| 配置 | 正确率 [95% CI] | 延迟 P50 | 每题费用（标价） |
|---|---|---|---|
| 按节点路由（基线） | 79% [69–88%] | 11.6 s | $0.00145 |
| 全部云端（基线） | 88% [79–96%] | 6.7 s | $0.00246 |
| 全部本地 | 63% [51–73%] | 10.9 s | $0 |
| 按节点路由（当前：加固 + 温度 0 + 关闭思考 + 注入防护） | 85% [76–93%] | 8.9 s | $0.00087 |
| 全部云端（加固 + 温度 0 + 关闭思考，注入防护前的提示词） | 87% [78–94%] | 4.5 s | $0.00190 |

- **调参前后**（同一批题配对比较）：按节点路由每题费用下降 40%，平均延迟少 3.3 秒，正确率 +6 个点但不显著（95% CI [−1.5, +14.9]）；忠实度下降 1.8 个点（CI [−3.8, −0.4]），幅度小但统计上显著。全部云端费用下降 23%、平均延迟少 2.3 秒，正确率无显著变化。
- **稳定性**（17 题 × 5 次）：查询与反思温度从 1.0 改为 0 后，5 次全对的题目从 82% 升到 94%，同一问题两次检索到的片段集合 Jaccard 从 0.66 变为 1.00。
- **失败归因**：答错的题里，最主要的原因是检索没有召回证据（基线按节点路由答错的 13 题中占 6 题），其次是证据已在上下文中但回答不完整。只用原问题检索时 Recall@5 为 62%，经过 Agent 的查询改写和反思补检，证据召回提高到约 70%。
- **相似度阈值**：0.5 的阈值几乎不起过滤作用（无答案题中 83% 仍有片段通过）；提高到 0.65 才能让多数无答案题检索不到片段，但证据召回会下降 14 个点，因此拒答主要依靠回答模型判断。
- **间接提示注入**：防护前，关闭思考的全部云端配置有 1/2 道注入题中招（开思考时 2/2 通过），全部本地 0/2；加上防护后，按节点路由与全部云端各跑 3 次、共 3 道注入题（含按前端默认参数复现的一道），均 9/9 通过，输出端链接过滤没有被触发，作为兜底保留。
- **故障注入**（10 个场景）：加固前 6 个场景整次运行失败，其中向量库不可达时每次运行要卡 262 秒；当前代码 10 个场景 30 次运行全部给出回答（必要时降级），且在前端使用的 `messages` 流式模式下运行状态均为成功。实验中 DeepSeek 账户真实欠费（HTTP 402），加固前 228 次运行全部失败；加固后用故障注入复现同一故障，由本地模型降级作答。
- **访问控制**：18 项检查全部通过，包括没有凭证与客户端自造会话被拒绝、匿名访客之间以及匿名访客与 ops 之间的线程隔离、伪造身份与分组、受限令牌调用白名单外模型、跨令牌查询成本。
- **裁判可信度**：从裁判的要点判定中分层抽取 30 条做人工盲评，完全一致 83%（Cohen's κ = 0.75），“是否覆盖”一致 97%；分歧都在相邻等级，裁判偏严和偏宽的条数相近（3 比 2），按人工判定改判后没有一道题的对错发生变化。

## 下一步

- 混合检索与重排：检索未召回是答错的首要原因。在向量检索之外加关键词检索，用 RRF 融合两路结果，再接 reranker 重排，用现有评测集衡量效果。
- 无答案题提前结束：本地 4B 模型做反思时，对知识库没有覆盖的问题常常用满全部轮数，可以在首轮检索全部低于阈值时提前收敛。
- Go 入库服务：文档上传后经 Redis Streams 异步切块、向量化。

## 快速开始

依赖：Docker、Go 1.26、bun、Node 20+、Python 3.11+、Ollama。

```bash
git clone --recurse-submodules https://github.com/ZSYcho/llm-gateway-rag-agent.git
cd llm-gateway-rag-agent

# 1. 本地模型。6GB 显存：OLLAMA_CONTEXT_LENGTH=8192；LLAMA_ARG_CACHE_RAM=1024 限制推理进程的提示缓存内存
#    （默认 8GB）；bge-m3 设 num_ctx 2048，否则它与 qwen3 不能同时驻留显存，会反复加载
ollama pull qwen3:4b-instruct-2507-q4_K_M && ollama pull bge-m3

# 2. 网关 + 数据层
./scripts/build.sh            # 前端构建 + Linux 静态二进制
docker compose up -d --build  # → http://localhost:3000
# 在网关后台：初始化管理员；添加 Ollama 渠道（http://host.docker.internal:11434）
# 和 DeepSeek 渠道（deepseek-flash）；在「模型定价」中为模型配置价格；新建令牌

# 3. Agent 后端
cd repos/gemini-fullstack-langgraph-quickstart/backend
python -m venv .venv && .venv/Scripts/pip install -e . "langgraph-cli[inmem]" colorama
cp .env.example .env              # 填入网关令牌 GATEWAY_API_KEY
../../../scripts/fetch-corpus.sh  # 语料 → runtime/corpus
PYTHONUTF8=1 PYTHONPATH=src .venv/Scripts/python -m agent.kb_ingest --corpus-dir ../../../runtime/corpus
# 可选：AGENT_USERS_FILE 指向 API 密钥 → 身份与分组的映射（见 .env.example）；不设置时所有调用方只能读公开文档
PYTHONUTF8=1 .venv/Scripts/langgraph dev --no-browser --port 2024

# 4. 前端
cd ../frontend && npm ci && npm run dev   # → http://localhost:5173/app/
```

Windows 下可先 `source dev-env.sh`，将构建缓存放到非系统盘并开启 Python UTF-8 模式。评测的运行方式见 [eval/README.md](eval/README.md)。

## 文档

- [docs/troubleshooting.md](docs/troubleshooting.md)：部署与调试记录（Docker 镜像拉取失败定位到 USB 网卡驱动、结构化输出、上游 bug 等）。
- [eval/README.md](eval/README.md)：评测设计、数据集、指标定义与运行方式；[eval/results/SUMMARY.md](eval/results/SUMMARY.md)：评测结果。
