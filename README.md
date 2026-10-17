# llm-gateway-rag-agent

LLM 网关驱动的 RAG 研究型 Agent 平台：所有模型调用经过 Go 网关统一鉴权、路由和计量，LangGraph Agent 在 pgvector 知识库上多步检索、反思补检，并给出带引用的回答；按节点选择本地模型或云模型。

基于两个开源项目二次开发（以 git submodule 引入，改动在各自 fork 的分支上）：

| 子模块 | 上游 | 作用 |
|---|---|---|
| `repos/new-api` | [QuantumNous/new-api](https://github.com/QuantumNous/new-api)（Go, AGPL-3.0） | OpenAI 兼容网关：渠道管理、令牌、计费、日志 |
| `repos/gemini-fullstack-langgraph-quickstart` | [google-gemini/gemini-fullstack-langgraph-quickstart](https://github.com/google-gemini/gemini-fullstack-langgraph-quickstart)（Apache-2.0） | LangGraph 研究型 Agent + React 前端 |

## 架构

```
React 前端 (:5173) ──► LangGraph API (:2024)
                         generate_query → kb_retrieve ×N → reflection ⟲ → finalize_answer
                         每次调用带 X-Agent-Run-Id / X-Agent-Node 请求头
                             │ OpenAI 兼容协议         │ 余弦 top-k
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
- **网关侧 Agent 成本归因**：new-api 在消费日志中记录 `X-Agent-Run-Id` / `X-Agent-Node`，写进日志的 `other` JSON 字段，不改表结构。新增接口 `GET /api/agent_runs/:id/cost`，用令牌鉴权，只能查本令牌的记录，按节点汇总调用数、token、费用和耗时。`:id` 是 LangGraph 的 run_id，即一次提问对应一个 run。
- **模型定价**：本地模型（Qwen、bge-m3）定价为 0；云端回答模型使用 DeepSeek 当前的模型名 `deepseek-flash`，按官方高峰价配置（输入 $0.3、输出 $1.2、缓存命中 $0.006，单位均为每百万 token）。例如一次按节点路由的提问共 4 次调用，只有回答节点产生费用，约 $0.002。
- **前端**：KaTeX 公式与 GFM 表格渲染；时间线显示每次检索的片段数、最高相似度、来源，以及反思给出的补充查询。
- **一键部署（网关 + 数据层）**：`docker-compose.yml` 启动 PostgreSQL（pgvector）、Redis、new-api；new-api 镜像由宿主机交叉编译的静态二进制 + alpine 构成。

## 进行中

- 评测集：检索 hit@k / MRR，对比全云端 / 按节点路由 / 路由 + 缓存的质量、延迟与成本。
- 混合检索与重排：在向量检索之外加关键词检索，用 RRF 融合两路结果，再接 reranker 重排。
- Go 入库服务：文档上传后经 Redis Streams 异步切块、向量化。

## 快速开始

依赖：Docker、Go 1.26、bun、Node 20+、Python 3.11+、Ollama。

```bash
git clone --recurse-submodules https://github.com/ZSYcho/llm-gateway-rag-agent.git
cd llm-gateway-rag-agent

# 1. 本地模型（6GB 显存建议设置 OLLAMA_CONTEXT_LENGTH=8192 后重启 Ollama）
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
PYTHONUTF8=1 .venv/Scripts/langgraph dev --no-browser --port 2024

# 4. 前端
cd ../frontend && npm ci && npm run dev   # → http://localhost:5173/app/
```

Windows 下可先 `source dev-env.sh`，将构建缓存放到非系统盘并开启 Python UTF-8 模式。

## 文档

- [docs/troubleshooting.md](docs/troubleshooting.md)：部署与调试记录（Docker 镜像拉取失败定位到 USB 网卡驱动、结构化输出、上游 bug 等）。
