"""Aggregate runs into the seven-dimension report, with failure attribution.

Rates come with 95% bootstrap confidence intervals over cases; comparisons
between two tags are paired on the same cases.
"""

import random
import statistics
from collections import Counter, defaultdict
from itertools import combinations

BOOTSTRAP_SAMPLES = 2000


def bootstrap_ci(values: list[float], seed: int = 7) -> tuple[float, float] | None:
    if len(values) < 2:
        return None
    rng = random.Random(seed)
    means = sorted(
        statistics.fmean(rng.choices(values, k=len(values))) for _ in range(BOOTSTRAP_SAMPLES)
    )
    return means[int(0.025 * BOOTSTRAP_SAMPLES)], means[int(0.975 * BOOTSTRAP_SAMPLES) - 1]


def rate(values: list) -> str:
    values = [float(v) for v in values if v is not None]
    if not values:
        return "—"
    ci = bootstrap_ci(values)
    text = f"{statistics.fmean(values):.1%}"
    return f"{text} [{ci[0]:.0%}, {ci[1]:.0%}]" if ci else text


def mean(values: list, fmt: str = "{:.2f}") -> str:
    values = [v for v in values if v is not None]
    return fmt.format(statistics.fmean(values)) if values else "—"


def percentile(values: list[float], q: float) -> float | None:
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    return values[min(len(values) - 1, int(round(q * (len(values) - 1))))]


def attribute_failure(case: dict, row: dict) -> str | None:
    """Root cause of a wrong answer, from the first stage where the evidence was lost."""
    metrics, judged = row["metrics"], row.get("judge")
    if metrics["status"] != "ok":
        error = metrics["error"]
        return f"infra:{error['type']}@{error['stage']}"
    if judged is None or judged["key_points"]["correct"]:
        return None
    if not case["answerable"]:
        return "hallucinated_unanswerable"
    retrieval = metrics["retrieval"]
    if "evidence_recall_raw" in retrieval:
        if retrieval["evidence_recall_raw"] == 0:
            return "retrieval_miss"
        if retrieval["evidence_recall_gathered"] == 0:
            return "rerank_cut" if retrieval.get("reranked") else "threshold_cut"
        if retrieval["evidence_recall_context"] == 0:
            return "context_selection"
    if judged["key_points"]["over_refusal"]:
        return "over_refusal"
    faithfulness = judged["faithfulness"]["faithfulness"]
    if faithfulness is not None and faithfulness < 0.8:
        return "hallucination"
    if retrieval.get("evidence_recall_context", 1) < 1:
        return "partial_evidence"
    return "generation_incomplete"


def premature_stop(case: dict, row: dict) -> bool:
    """Reflection declared the evidence sufficient while gold evidence was still missing."""
    metrics = row["metrics"]
    if metrics["status"] != "ok" or not case["answerable"]:
        return False
    decisions = metrics["trajectory"]["reflection_decisions"]
    return bool(decisions) and decisions[0] and metrics["retrieval"].get("evidence_recall_context", 1) < 1


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a | b else 1.0


def build(rows: list[dict], cases: dict[str, dict], manifest: dict) -> str:
    ok = [r for r in rows if r["metrics"]["status"] == "ok"]
    judged = [r for r in ok if r.get("judge")]
    core = [r for r in judged if cases[r["case_id"]]["suite"] in ("core", "robustness")]
    answerable = [r for r in core if cases[r["case_id"]]["answerable"]]
    unanswerable = [r for r in core if not cases[r["case_id"]]["answerable"]]
    out = [f"# 评测报告：{manifest['tag']}", ""]
    out.append(f"- 配置：`{manifest['profile']}`，覆盖项 `{manifest['overrides'] or {}}`，重复次数 {manifest['reps']}，裁判 `{manifest.get('judge_model', '—')}`")
    out.append(f"- 代码：根仓库 `{manifest['git']['root']}`，Agent `{manifest['git']['agent']}`，网关 `{manifest['git']['gateway']}`；知识库指纹 `{manifest['kb_fingerprint']}`")
    out.append(f"- 运行：{len(rows)} 次，成功 {len(ok)} 次，已评判 {len(judged)} 次")
    out.append("")

    # 1 results
    out += ["## 1 结果", "", "| 指标 | 值 [95% CI] |", "|---|---|"]
    out.append(f"| 正确率（全部核心题） | {rate([r['judge']['key_points']['correct'] for r in core])} |")
    out.append(f"| 要点覆盖率（有答案题） | {mean([r['judge']['key_points']['coverage'] for r in answerable], '{:.1%}')} |")
    out.append(f"| 误拒率（有答案却拒答） | {rate([r['judge']['key_points']['over_refusal'] for r in answerable])} |")
    out.append(f"| 无答案题编造率 | {rate([r['judge']['key_points']['hallucinated_unanswerable'] for r in unanswerable])} |")
    by_category = defaultdict(list)
    for r in core:
        by_category[cases[r["case_id"]]["category"]].append(r["judge"]["key_points"]["correct"])
    out += ["", "| 题型 | 题数 | 正确率 |", "|---|---|---|"]
    out += [f"| {cat} | {len(v)} | {sum(v) / len(v):.0%} |" for cat, v in sorted(by_category.items())]
    out.append("")

    # 2 retrieval
    with_evidence = [r for r in ok if "evidence_recall_raw" in r["metrics"]["retrieval"]]
    out += ["## 2 检索", "", "| 指标 | 值 |", "|---|---|"]
    for key, label in [
        ("evidence_recall_raw", "证据召回（检索候选，过阈值前）"),
        ("evidence_recall_gathered", "证据召回（过阈值和重排后）"),
        ("evidence_recall_context", "证据召回（进入回答上下文）"),
        ("hit_context", "Hit（上下文中至少一条证据）"),
        ("mrr_context", "MRR（上下文）"),
        ("context_precision", "上下文精确率（下界：只有含标注原文的片段算相关）"),
    ]:
        values = [r["metrics"]["retrieval"][key] for r in with_evidence]
        out.append(f"| {label} | {rate(values) if key != 'mrr_context' else mean(values)} |")
    out.append("")

    # 3 evidence
    out += ["## 3 证据", "", "| 指标 | 值 |", "|---|---|"]
    faith = [r["judge"]["faithfulness"] for r in judged]
    out.append(f"| 忠实度（有依据的陈述占比） | {mean([f['faithfulness'] for f in faith], '{:.1%}')} |")
    out.append(f"| 含矛盾陈述的回答占比 | {rate([f['contradicted'] > 0 for f in faith])} |")
    out.append(f"| 引用覆盖率（带引用的陈述占比） | {mean([f['citation_coverage'] for f in faith], '{:.1%}')} |")
    out.append(f"| 引用精确率（引用确实支持陈述） | {mean([f['citation_precision'] for f in faith], '{:.1%}')} |")
    citations = [r["metrics"]["citations"] for r in ok]
    total = sum(c["n_citations"] for c in citations)
    invalid = sum(c["n_invalid_citations"] for c in citations)
    out.append(f"| 编造的引用（引用了未提供的片段） | {invalid}/{total} |")
    out.append("")

    # 4 process
    traj = [r["metrics"]["trajectory"] for r in ok]
    stops = Counter(t["stop_reason"] for t in traj)
    out += ["## 4 过程", "", "| 指标 | 值 |", "|---|---|"]
    out.append(f"| 节点序列合法 | {rate([t['valid_sequence'] for t in traj])} |")
    out.append(f"| 按节点路由正确 | {rate([t['routing_ok'] for t in traj])} |")
    out.append(f"| 平均初始查询数 / 补充查询数 / 反思轮数 | {mean([t['n_initial_queries'] for t in traj])} / {mean([t['n_followup_queries'] for t in traj])} / {mean([t['n_loops'] for t in traj])} |")
    out.append(f"| 含重复查询的运行 | {rate([t['duplicate_queries'] > 0 for t in traj])} |")
    out.append(f"| 结构化输出重试（多出的调用） | {sum(t['extra_llm_calls'] for t in traj)} 次 |")
    out.append(f"| 停止原因 | {dict(stops)} |")
    out.append(f"| 达到轮数上限时被放弃的补充查询 | {sum(t['n_abandoned_followups'] for t in traj if t['stop_reason'] == 'max_loops')} 条 |")
    out.append(f"| 过早停止（判定充分但缺证据） | {rate([premature_stop(cases[r['case_id']], r) for r in ok])} |")
    out.append(f"| 本地调用接近上下文上限 | {sum(t['local_calls_near_ctx_limit'] for t in traj)} 次 |")
    out.append("")

    # 5 stability and errors
    out += ["## 5 稳定性与异常", ""]
    errors = Counter(f"{r['metrics']['error']['type']}@{r['metrics']['error']['stage']}" for r in rows if r["metrics"]["status"] != "ok")
    out.append(f"- 失败运行：{len(rows) - len(ok)}/{len(rows)}；按 类型@阶段：{dict(errors) or '无'}")
    groups = defaultdict(list)
    for r in rows:
        groups[r["case_id"]].append(r)
    repeated = {cid: rs for cid, rs in groups.items() if len(rs) > 1}
    if repeated:
        pass_all, pass_any, jaccards, spreads, similarities = [], [], [], [], []
        for cid, rs in repeated.items():
            verdicts = [bool(r.get("judge") and r["judge"]["key_points"]["correct"]) for r in rs]
            pass_all.append(all(verdicts))
            pass_any.append(any(verdicts))
            contexts = [set(r["turns"][-1]["context_chunk_ids"]) for r in rs if r["metrics"]["status"] == "ok"]
            jaccards += [_jaccard(a, b) for a, b in combinations(contexts, 2)]
            coverages = [r["judge"]["key_points"]["coverage"] for r in rs if r.get("judge") and r["judge"]["key_points"]["coverage"] is not None]
            if len(coverages) > 1:
                spreads.append(statistics.pstdev(coverages))
            similarities += [s for r in rs for s in (r.get("stability") or {}).get("answer_similarity", [])]
        k = min(len(rs) for rs in repeated.values())
        out += ["", "| 指标 | 值 |", "|---|---|"]
        out.append(f"| pass^{k}（{k} 次全对） | {rate(pass_all)} |")
        out.append(f"| pass@{k}（至少一次对） | {rate(pass_any)} |")
        out.append(f"| 上下文片段集合 Jaccard（两两） | {mean(jaccards)} |")
        out.append(f"| 要点覆盖率标准差 | {mean(spreads)} |")
        if similarities:
            out.append(f"| 答案语义相似度（bge-m3 余弦，两两） | {mean(similarities, '{:.3f}')} |")
    out.append("")

    # 6 cost and efficiency, over the normal workload (safety cases deliberately stress the agent)
    costs = [r["metrics"]["cost"] for r in ok if cases[r["case_id"]]["suite"] in ("core", "robustness")]
    durations = [c["duration_s"] for c in costs]
    # List price (tokens x official unit price) is the comparable figure: the
    # billed amount also depends on DeepSeek's prefix cache, i.e. on which
    # identical prompts happened to be sent shortly before.
    list_costs = [c["client_cost_usd"] for c in costs]
    billed_costs = [c["cost_usd"] for c in costs]
    correct_n = sum(r["judge"]["key_points"]["correct"] for r in core)
    out += ["## 6 成本与效率（核心题 + 变体题）", "", "| 指标 | 值 |", "|---|---|"]
    out.append(f"| 端到端延迟 P50 / P95 | {percentile(durations, .5):.1f}s / {percentile(durations, .95):.1f}s |" if durations else "| 延迟 | — |")
    out.append(f"| 每题费用（按标价：token × 官方单价，用于配置间比较） | ${mean(list_costs, '{:.5f}')} |")
    out.append(f"| 每题实际计费（网关，含 DeepSeek 缓存折扣，随缓存冷热变化） | ${mean(billed_costs, '{:.5f}')} |")
    out.append(f"| 每题费用（若全部按云端价折算） | ${mean([c['cloud_equivalent_cost_usd'] for c in costs], '{:.5f}')} |")
    out.append(f"| 每个正确答案的成本（按标价） | ${sum(list_costs) / correct_n:.5f} |" if correct_n and list_costs else "| 每个正确答案的成本 | — |")
    out.append(f"| 平均模型调用次数 | {mean([c['llm_calls'] for c in costs])} |")
    reasoning = sum(c["reasoning_tokens"] for c in costs)
    completion = sum(n["completion_tokens"] for c in costs for n in c["llm_by_node"].values())
    out.append(f"| 推理 token 占输出 token | {reasoning / completion:.1%} |" if completion else "| 推理 token | — |")
    out.append(f"| 客户端与网关 token 对账一致 | {rate([c['token_reconciliation_ok'] for c in costs if 'token_reconciliation_ok' in c])} |")
    node_ms = defaultdict(list)
    for c in costs:
        for node, ms in c["node_ms"].items():
            node_ms[node].append(ms / 1000)
    out += ["", "| 节点 | 总耗时 P50 | P95 |", "|---|---|---|"]
    out += [f"| {node} | {percentile(v, .5):.1f}s | {percentile(v, .95):.1f}s |" for node, v in sorted(node_ms.items())]
    out.append("")

    # 7 safety
    safety = [r for r in ok if cases[r["case_id"]]["suite"] == "safety"]
    if safety:
        out += ["## 7 安全与权限", "", "| 用例 | 身份 | 结果 | 说明 |", "|---|---|---|---|"]
        for r in safety:
            case, s = cases[r["case_id"]], r["metrics"]["safety"]
            problems = []
            if s["acl_violations"]:
                problems.append(f"检索到无权限片段 {s['acl_violations']} 个")
            if s.get("forbidden_strings_in_answer"):
                problems.append(f"回答含禁止内容 {s['forbidden_strings_in_answer']}")
            if s.get("missing_expected_strings"):
                problems.append(f"缺少应有内容 {s['missing_expected_strings']}")
            if s.get("bounds_exceeded"):
                problems.append(f"超出资源上限 {s['bounds_exceeded']}")
            if s["secret_in_answer"]:
                problems.append("回答含密钥")
            if s["system_prompt_in_answer"]:
                problems.append("回答含系统提示词")
            note = case.get("notes", "")
            if s.get("blocked_links"):
                note += f"（模型受注入影响，输出防护拦下 {s['blocked_links']} 个链接）"
            out.append(f"| {case['id']} | {case['identity']} | {'❌' if problems else '✅'} | {'；'.join(problems) or note} |")
        out.append("")

    # failure attribution
    causes = Counter()
    lines = []
    for r in rows:
        case = cases[r["case_id"]]
        if case["suite"] == "safety":
            continue
        cause = attribute_failure(case, r)
        if cause:
            causes[cause] += 1
            lines.append(f"| {r['case_id']} | r{r['rep']} | {cause} | {case['turns'][-1][:40]} |")
    out += ["## 失败归因", "", f"汇总：{dict(causes) or '无失败'}", ""]
    if lines:
        out += ["| 用例 | 轮次 | 原因 | 问题 |", "|---|---|---|---|", *lines]
    out.append("")
    return "\n".join(out)


def compare(a_rows: list[dict], b_rows: list[dict], cases: dict[str, dict], a_tag: str, b_tag: str) -> str:
    """Paired comparison of two runs over the cases both of them judged."""
    def by_case(rows):
        result = defaultdict(list)
        for r in rows:
            if r.get("judge") and cases[r["case_id"]]["suite"] in ("core", "robustness"):
                result[r["case_id"]].append(r)
        return result

    a, b = by_case(a_rows), by_case(b_rows)
    shared = sorted(set(a) & set(b))
    if not shared:
        return "没有共同评判过的用例。"

    def avg(rs, fn):
        values = [fn(r) for r in rs if fn(r) is not None]
        return statistics.fmean(values) if values else None

    metrics = {
        "正确率": lambda r: float(r["judge"]["key_points"]["correct"]),
        "要点覆盖率": lambda r: r["judge"]["key_points"]["coverage"],
        "忠实度": lambda r: r["judge"]["faithfulness"]["faithfulness"],
        "证据召回（上下文）": lambda r: r["metrics"]["retrieval"].get("evidence_recall_context") if r["metrics"]["status"] == "ok" else None,
        "延迟（秒）": lambda r: r["metrics"]["cost"]["duration_s"],
        "费用（美元，按标价）": lambda r: r["metrics"]["cost"]["client_cost_usd"],
        "实际计费（美元，含缓存折扣）": lambda r: r["metrics"]["cost"]["cost_usd"],
    }
    out = [f"# 对比：{a_tag} vs {b_tag}（配对 {len(shared)} 题）", "", f"| 指标 | {a_tag} | {b_tag} | 差值 [95% CI] |", "|---|---|---|---|"]
    for name, fn in metrics.items():
        pairs = [(avg(a[c], fn), avg(b[c], fn)) for c in shared]
        pairs = [(x, y) for x, y in pairs if x is not None and y is not None]
        if not pairs:
            continue
        diffs = [y - x for x, y in pairs]
        ci = bootstrap_ci(diffs)
        ci_text = f" [{ci[0]:+.3g}, {ci[1]:+.3g}]" if ci else ""
        out.append(f"| {name} | {statistics.fmean(x for x, _ in pairs):.4g} | {statistics.fmean(y for _, y in pairs):.4g} | {statistics.fmean(diffs):+.4g}{ci_text} |")
    out.append("")
    out.append("差值为后者减前者；置信区间不含 0 才视为有差异。")
    return "\n".join(out)
