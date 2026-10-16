# llm-gateway-rag-agent

LLM 网关驱动的研究型 Agent 平台：所有模型调用经过 Go 网关统一鉴权、路由和计量，LangGraph Agent 按节点选择本地模型或云模型。

基于两个开源项目二次开发（以 git submodule 引入，改动在各自 fork 的分支上）：

| 子模块 | 上游 | 作用 |
|---|---|---|
| `repos/new-api` | [QuantumNous/new-api](https://github.com/QuantumNous/new-api)（Go, AGPL-3.0） | OpenAI 兼容网关：渠道管理、令牌、计费、日志 |
| `repos/gemini-fullstack-langgraph-quickstart` | [google-gemini/gemini-fullstack-langgraph-quickstart](https://github.com/google-gemini/gemini-fullstack-langgraph-quickstart)（Apache-2.0） | LangGraph 研究型 Agent + React 前端 |

## 架构

```
React 前端 (:5173) ──► LangGraph API (:2024)
                         generate_query → retrieve → reflection ⟲ → finalize_answer
                         每次调用带 X-Agent-Run-Id / X-Agent-Node 请求头
                                   │ OpenAI 兼容协议
                                   ▼
                       new-api Go 网关 (:3000)  ── PostgreSQL / Redis
                         ├─ Ollama 本地 (GPU)：qwen3:4b-instruct（查询生成、反思）, bge-m3（向量）
                         └─ DeepSeek 云端：deepseek-chat（最终回答）
```

## 已实现

- **统一网关接入**：Agent 的全部 LLM 调用改为经 new-api 转发（`backend/src/agent/llm.py`），Agent 只持有网关令牌，不接触上游厂商 Key。
- **按节点路由模型**：查询生成与反思走本地 4B 量化模型，最终回答走云模型；前端可切换 Auto / 全本地 / 全云端，用于对比效果与成本。
- **本地模型稳定的结构化输出**：Ollama 不支持强制 `tool_choice`，function calling 偶发无工具调用；本地模型改用 `json_schema`（网关映射为 Ollama 约束解码），云模型用 function calling，解析为空时重试后显式报错。
- **修复上游两个 bug**：研究轮数上限 `max_research_loops` 不生效；反思给出空的补充查询时图无回答静默结束。
- **前端**：KaTeX 公式与 GFM 表格渲染。
- **一键部署（网关 + 数据层）**：`docker-compose.yml` 启动 PostgreSQL（pgvector）、Redis、new-api；new-api 镜像由宿主机交叉编译的静态二进制 + alpine 构成。

## 进行中

- 知识库检索节点（pgvector + bge-m3），替换当前占位的检索节点，回答附引用来源。
- 网关侧 Agent 成本归因：按 run / 节点汇总 token 与费用。
- 评测集：对比全云端 / 按节点路由 / 路由 + 缓存的质量、延迟与成本。

## 快速开始

依赖：Docker、Go 1.26、bun、Node 20+、Python 3.11+、Ollama。

```bash
git clone --recurse-submodules https://github.com/ZSYcho/llm-gateway-rag-agent.git
cd llm-gateway-rag-agent

# 1. 本地模型
ollama pull qwen3:4b-instruct-2507-q4_K_M && ollama pull bge-m3

# 2. 网关 + 数据层
./scripts/build.sh            # 前端构建 + Linux 静态二进制
docker compose up -d --build  # → http://localhost:3000
# 在网关后台：初始化管理员；添加 Ollama 渠道（http://host.docker.internal:11434）
# 和 DeepSeek 渠道（deepseek-chat）；新建令牌

# 3. Agent 后端
cd repos/gemini-fullstack-langgraph-quickstart/backend
python -m venv .venv && .venv/Scripts/pip install -e . langchain-openai "langgraph-cli[inmem]" colorama
echo "GATEWAY_API_KEY=sk-..." >> .env
PYTHONUTF8=1 .venv/Scripts/langgraph dev --no-browser --port 2024

# 4. 前端
cd ../frontend && npm ci && npm run dev   # → http://localhost:5173/app/
```

Windows 下可先 `source dev-env.sh`，将构建缓存放到非系统盘并开启 Python UTF-8 模式。

## 文档

- [docs/troubleshooting.md](docs/troubleshooting.md)：部署与调试记录（Docker 镜像拉取失败定位到 USB 网卡驱动、结构化输出、上游 bug 等）。
