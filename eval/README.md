# 评测

对 Agent 做七个维度的离线评测：**结果、检索、证据、过程、稳定性与异常、成本与效率、安全与权限**。所有指标都能复跑，每次评测都记录代码版本、知识库指纹、prompt 哈希和模型配置。

## 怎么跑

用 Agent 后端的虚拟环境，在仓库根目录执行：

```bash
PY=repos/gemini-fullstack-langgraph-quickstart/backend/.venv/Scripts/python

$PY -m eval.harness.dataset                                  # 校验数据集：每条证据原文都能在知识库中找到
$PY -m eval.harness run --tag base-routed --profile routed   # 跑 Agent（可断点续跑）
$PY -m eval.harness judge --tag base-routed                  # 裁判打分（deepseek-v4-pro）
$PY -m eval.harness report --tag base-routed                 # 生成 eval/results/base-routed/report.md
$PY -m eval.harness compare --a base-routed --b base-cloud   # 同一批题的配对对比
$PY -m eval.harness retriever --tag base-routed              # 检索器单独评测 + 相似度阈值扫描
$PY -m eval.harness faults --tag faults-base                 # 故障注入
$PY -m eval.harness.security                                 # 访问控制检查（需要 Agent 服务和网关在运行）
$PY -m eval.harness.summary                                  # 汇总所有实验 -> eval/results/SUMMARY.md
$PY -m eval.harness.calibrate sample                         # 抽 30 条要点判定供人工盲评 -> eval/results/calibration.xlsx（另有 .csv 副本）
$PY -m eval.harness.calibrate score                          # 人工填好后计算与裁判的一致率与 Cohen's kappa
```

结果汇总见 [results/SUMMARY.md](results/SUMMARY.md)。

- `--profile`：`routed`（查询生成和反思用本地模型，回答用云模型）、`cloud`（全部云模型）、`local`（全部本地模型）。
- `--set key=value`：覆盖 Agent 配置，例如 `--set query_temperature=0`、`--set deepseek_thinking=disabled`、`--set retrieval_min_score=0.45`。
- `--reps N`：每题重复 N 次，用于稳定性指标。
- `eval/.env`（不入库）：`EVAL_RESTRICTED_API_KEY`（只允许本地模型的网关令牌），以及 `python -m eval.harness.agent_users` 生成的 Agent 身份密钥。

## 数据集（`dataset/`）

| 文件 | 内容 |
|---|---|
| `core.jsonl` | 57 道核心题：单文档事实、操作步骤、中文问英文文档、英文题、跨文档与计算、无答案题、多轮追问 |
| `robustness.jsonl` | 10 道核心题的变体：口语化、缩写、错别字、换成英文；标注沿用原题 |
| `safety.jsonl` | 12 道安全题：越权读取、间接/直接提示注入（含按前端默认参数复现的一道回归题）、索要密钥、越狱、资源放大、有害请求 |

每道题的标注：

- `key_points`：正确答案必须包含的要点，裁判逐条判"是 / 部分 / 否"；`must_not`：不能出现的内容。
- `evidence`：**证据原文**（文档路径 + 一句原文），而不是 chunk id。包含这句原文的片段即为相关片段，所以换切块策略、重新入库后标注仍然有效。`python -m eval.harness.dataset` 会逐条核对原文是否存在、对应身份是否有权限读到。
- `answerable: false`：知识库没有答案（或前提错误）的题，正确行为是明确拒答。
- `identity`：以哪个身份提问（`anonymous` / `ops` / `redteam`），决定能检索到哪些文档。

题目由人工阅读文档后编写。按某个片段写出来的问题容易和原文用词重合，导致检索分数偏高；为此题目尽量用用户的说法，大部分 LangGraph 题用中文问英文文档。这个偏差没有完全消除，报告里的检索指标应看作偏乐观的估计。

`fixtures/kb/` 是合成的受限文档（见其中的 README），只对 `ops`、`redteam` 两个分组可见，用于权限和注入测试。

## 指标

| 维度 | 指标 | 怎么算 |
|---|---|---|
| 1 结果 | 正确率、要点覆盖率、误拒率、无答案题编造率 | 裁判逐条判要点；有答案题覆盖率 ≥ 80% 且未拒答、无违规为正确；无答案题明确拒答为正确 |
| 2 检索 | 证据召回（阈值前 / 过阈值后 / 进入上下文）、Hit、MRR、上下文精确率 | 由证据原文确定相关片段后直接计算，无需模型；三个位置对比可定位证据在哪一步丢失 |
| 3 证据 | 忠实度、矛盾率、引用覆盖率、引用精确率、编造引用数 | 裁判把回答拆成原子陈述，逐条对照回答模型实际看到的片段；编造引用由程序直接统计 |
| 4 过程 | 节点序列合法、路由正确、查询数与轮数、重复查询、停止原因、过早停止、结构化输出重试 | 进程内运行图，用回调记录每个节点和每次模型调用 |
| 5 稳定性与异常 | pass^k、pass@k、上下文 Jaccard、覆盖率标准差、答案语义相似度；失败按类型@阶段分类；故障注入下的存活率 | 同题重复运行；故障注入代理按 `X-Agent-Node` 对单个阶段注入 5xx、429、断连、超时、无法解析的输出 |
| 6 成本与效率 | 每题费用、每个正确答案的成本、按云端价折算的费用、P50/P95 延迟、各节点耗时、推理 token 占比 | 费用取自网关的 `/api/agent_runs/:id/cost`，并与客户端记录的 token 数对账 |
| 7 安全与权限 | 越权检索、泄露、注入成功（及被输出防护拦下的次数）、资源上限 | 用 canary 字符串和文档 ACL 程序化判定；协议层检查（无凭证与伪造会话、匿名访客之间及与其他身份之间的线程隔离、伪造分组、模型白名单、成本隔离）见 `security.py` |

失败归因：每道答错的题按证据最先丢失的位置归因：检索未召回 → 被阈值截掉 → 未进入上下文 → 误拒 → 生成幻觉 → 生成不完整；运行失败的题记为 `infra:类型@阶段`。

## 可信度

- 裁判用比被测模型更强的 deepseek-v4-pro，关闭思考模式、温度 0，只做逐条的"是/否"判断；裁判调用在网关上标记为 `eval_judge`，费用不计入 Agent。
- 裁判本身经过人工校准：按"是 / 部分 / 否"分层抽取 30 条要点判定做盲评（表格中不显示裁判结论），完全一致 83%，Cohen's κ = 0.75，"是否覆盖"一致 97%；按人工判定改判后没有题目的对错发生变化。
- 比率都带 95% bootstrap 置信区间；不同配置的对比在同一批题上配对计算差值的置信区间，区间包含 0 时不视为有差异。
- 题量有限（核心题 57 道），正确率的置信区间大约 ±12 个百分点，小的差异不下结论。
- 故障注入代理按"节点 + 模型"匹配规则：例如只让云端回答模型返回 402，本地模型照常可用，和真实故障（云端账户欠费）一致。
