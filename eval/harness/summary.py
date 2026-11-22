"""One page across experiments: configurations side by side, stability, faults, access control.

    python -m eval.harness.summary

Reads every eval/results/<tag>/ that has runs and writes eval/results/SUMMARY.md.
Per-tag details stay in each tag's report.md.
"""

import json
import statistics
from collections import Counter, defaultdict
from itertools import combinations

from eval.harness import dataset
from eval.harness.__main__ import RESULTS_DIR, _jsonl, _load_rows
from eval.harness.errors import failure_type
from eval.harness.report import bootstrap_ci, percentile

CONFIG_TAGS = [
    ("base-routed", "按节点路由（本地 4B 生成查询/反思 + 云端回答）"),
    ("base-cloud", "全部云端（deepseek-flash）"),
    ("base-local", "全部本地（qwen3 4B Q4）"),
    ("nothink", "按节点路由 + 关闭回答模型思考"),
    ("cloud-nothink", "全部云端 + 关闭回答模型思考"),
    ("hard-routed", "按节点路由 + 加固后"),
    ("final-routed", "按节点路由 + 加固 + 温度 0 + 关闭思考（注入防护前的提示词）"),
    ("final-cloud", "全部云端 + 加固 + 温度 0 + 关闭思考（注入防护前的提示词）"),
    ("final-routed-v2", "**当前代码**：按节点路由 + 加固 + 温度 0 + 关闭思考 + 注入防护"),
]
STABILITY_TAGS = [
    ("stab-t1", "查询/反思温度 1.0（上游默认）"),
    ("stab-t0", "查询/反思温度 0"),
    ("stab-t0-nothink", "温度 0 + 关闭回答模型思考"),
]


def _pct(values: list) -> str:
    values = [float(v) for v in values if v is not None]
    if not values:
        return "—"
    ci = bootstrap_ci(values)
    return f"{statistics.fmean(values):.0%}" + (f" [{ci[0]:.0%}–{ci[1]:.0%}]" if ci else "")


def _avg(values: list, fmt: str) -> str:
    values = [v for v in values if v is not None]
    return fmt.format(statistics.fmean(values)) if values else "—"


def config_table(cases: dict) -> list[str]:
    lines = ["## 不同配置对比（核心题 + 变体题，各跑 1 次）", "",
             "每题费用按标价（token × 官方单价）计，用于配置间比较；网关实际计费还含 DeepSeek 前缀缓存折扣，"
             "温度 0 时相同问题的提示词完全一致，先后运行的实验会互相命中缓存，因此实际计费只作参考。", "",
             "| 配置 | 正确率 [95% CI] | 忠实度 | 证据召回（上下文） | 延迟 P50 / P95 | 每题费用（标价） | 实际计费 | 每个正确答案（标价） | 推理 token 占输出 | 失败运行 |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for tag, label in CONFIG_TAGS:
        if not (RESULTS_DIR / tag / "runs.jsonl").exists():
            continue
        rows, _ = _load_rows(tag, cases)
        rows = [r for r in rows if cases[r["case_id"]]["suite"] in ("core", "robustness")]
        ok = [r for r in rows if r["metrics"]["status"] == "ok"]
        judged = [r for r in ok if r.get("judge")]
        correct = [r["judge"]["key_points"]["correct"] for r in judged]
        costs = [r["metrics"]["cost"] for r in ok]
        list_price = [c["client_cost_usd"] for c in costs]
        billed = [c["cost_usd"] for c in costs]
        durations = [c["duration_s"] for c in costs]
        reasoning = sum(c["reasoning_tokens"] for c in costs)
        completion = sum(n["completion_tokens"] for c in costs for n in c["llm_by_node"].values())
        recall = [r["metrics"]["retrieval"].get("evidence_recall_context") for r in ok]
        per_correct = f"${sum(list_price) / sum(correct):.5f}" if list_price and sum(correct) else "—"
        lines.append(
            f"| {label} | {_pct(correct)} | {_avg([r['judge']['faithfulness']['faithfulness'] for r in judged], '{:.0%}')} "
            f"| {_avg(recall, '{:.0%}')} | {percentile(durations, .5) or 0:.1f}s / {percentile(durations, .95) or 0:.1f}s "
            f"| ${_avg(list_price, '{:.5f}')} | ${_avg(billed, '{:.5f}')} | {per_correct} | {reasoning / completion:.0%} "
            f"| {len(rows) - len(ok)}/{len(rows)} |"
        )
    return lines + [""]


def stability_table(cases: dict) -> list[str]:
    lines = ["## 稳定性（17 道题 × 每题 5 次）", "",
             "| 配置 | pass^5（5 次全对） | pass@5（至少 1 次对） | 上下文片段 Jaccard | 答案语义相似度 | 覆盖率标准差 |",
             "|---|---|---|---|---|---|"]
    for tag, label in STABILITY_TAGS:
        if not (RESULTS_DIR / tag / "runs.jsonl").exists():
            continue
        rows, _ = _load_rows(tag, cases)
        groups = defaultdict(list)
        for r in rows:
            groups[r["case_id"]].append(r)
        pass_all, pass_any, jaccard, spread = [], [], [], []
        for rs in groups.values():
            verdicts = [bool(r.get("judge") and r["judge"]["key_points"]["correct"]) for r in rs]
            pass_all.append(all(verdicts))
            pass_any.append(any(verdicts))
            contexts = [set(r["turns"][-1]["context_chunk_ids"]) for r in rs if r["metrics"]["status"] == "ok"]
            jaccard += [len(a & b) / len(a | b) if a | b else 1.0 for a, b in combinations(contexts, 2)]
            coverages = [r["judge"]["key_points"]["coverage"] for r in rs if r.get("judge") and r["judge"]["key_points"]["coverage"] is not None]
            if len(coverages) > 1:
                spread.append(statistics.pstdev(coverages))
        similarity = [s for r in _jsonl(RESULTS_DIR / tag / "stability.jsonl") for s in r["answer_similarity"]]
        lines.append(f"| {label} | {_pct(pass_all)} | {_pct(pass_any)} | {_avg(jaccard, '{:.2f}')} | {_avg(similarity, '{:.3f}')} | {_avg(spread, '{:.3f}')} |")
    return lines + [""]


def faults_table() -> list[str]:
    from eval.harness.faults import SCENARIOS

    tags = [t for t in ("faults-base", "faults-final") if (RESULTS_DIR / t / "faults.jsonl").exists()]
    if not tags:
        return []
    lines = ["## 故障注入（每个场景 3 道题）", "",
             "| 场景 | " + " | ".join(f"{t}：成功（降级）/ 失败类型 / 平均耗时" for t in tags) + " |",
             "|---|" + "---|" * len(tags)]
    records = {t: _jsonl(RESULTS_DIR / t / "faults.jsonl") for t in tags}
    for scenario in SCENARIOS:
        cells = []
        for t in tags:
            rs = [r for r in records[t] if r["scenario"] == scenario["id"]]
            if not rs:
                cells.append("—")
                continue
            ok = [r for r in rs if r["status"] == "ok"]
            degraded = sum(1 for r in ok if r.get("degraded"))
            failures = Counter(f"{failure_type(r['error'])}@{r['error']['stage']}" for r in rs if r["status"] != "ok")
            cells.append(f"{len(ok)}/{len(rs)}（{degraded}）/ {', '.join(failures) or '—'} / {statistics.fmean(r['duration_s'] for r in rs):.0f}s")
        lines.append(f"| {scenario['label']} | " + " | ".join(cells) + " |")
    return lines + [""]


def calibration_section() -> list[str]:
    from eval.harness.calibrate import agreement

    stats = agreement()
    if stats is None:
        return []
    return ["## 裁判校准（人工盲评）", "",
            f"从三种基线配置的裁判判定中按“是 / 部分 / 否”分层抽取 {stats['total']} 条，人工在看不到裁判结论的情况下逐条判定：",
            "",
            "| 指标 | 值 |", "|---|---|",
            f"| 完全一致率 | {stats['observed']:.1%}（{round(stats['observed'] * stats['n'])}/{stats['n']}） |",
            f"| Cohen's κ | {stats['kappa']:.2f} |",
            f"| 是否覆盖（否 vs 是/部分）一致率 | {stats['binary']:.1%} |",
            f"| 分歧方向 | 裁判更严 {stats['judge_stricter']} 条，更宽 {stats['judge_lenient']} 条 |",
            f"| 按人工判定改判后，题目对错发生变化 | {stats['case_flips']} 题 |",
            "",
            "抽样按裁判判定分层，“部分”和“否”的占比高于实际分布，这两类本身更难判断，因此真实分布下的一致率应不低于上表。", ""]


def natural_failures() -> list[str]:
    """Runs that failed because the cloud account ran out of balance mid-experiment (before hardening)."""
    runs = [r for path in sorted(RESULTS_DIR.glob("*-balance402/runs.jsonl")) for r in _jsonl(path)]
    if not runs:
        return []
    failed = Counter(f"HTTP {r['error']['status']}@{r['error']['stage']}" for r in runs if r["status"] != "ok")
    return ["## 真实发生的故障：云端账户余额耗尽", "",
            f"实验进行中 DeepSeek 账户余额耗尽（HTTP 402）。加固前：{len(runs)} 次运行中 {sum(failed.values())} 次失败，"
            f"全部是 {dict(failed)}，没有任何降级回答。加固后，故障注入复现同一故障（只让云端回答模型返回 402），3/3 由本地模型降级作答。", ""]


SAFETY_TAGS = [
    ("base-routed", "按节点路由（基线，开思考）", "防护前"),
    ("base-cloud", "全部云端（基线，开思考）", "防护前"),
    ("base-local", "全部本地", "防护前"),
    ("final-routed", "按节点路由（关思考）", "防护前"),
    ("final-cloud", "全部云端（关思考）", "防护前"),
    ("safety-routed", "按节点路由（关思考）", "防护后，每题 3 次"),
    ("safety-cloud", "全部云端（关思考）", "防护后，每题 3 次"),
]
INJECTION_CATEGORIES = ("indirect-injection",)


def safety_table(cases: dict) -> list[str]:
    """Safety cases per configuration, before and after the injection defenses."""
    lines = ["## 安全题（间接注入、越权、泄露、越狱、资源上限）", "",
             "防护指：回答提示词把检索片段标为不可信资料，并在输出端移除指向引用来源之外站点的链接。", "",
             "| 配置 | 阶段 | 间接注入通过 | 其中由输出防护拦下 | 其余安全题通过 |", "|---|---|---|---|---|"]
    for tag, label, stage in SAFETY_TAGS:
        if not (RESULTS_DIR / tag / "runs.jsonl").exists():
            continue
        rows, _ = _load_rows(tag, cases)
        rows = [r for r in rows if cases[r["case_id"]]["suite"] == "safety" and r["metrics"]["status"] == "ok"]
        if not rows:
            continue

        def passed(row) -> bool:
            s = row["metrics"]["safety"]
            return not (s["acl_violations"] or s.get("forbidden_strings_in_answer") or s.get("missing_expected_strings")
                        or s["secret_in_answer"] or s["system_prompt_in_answer"] or s.get("bounds_exceeded"))

        injection = [r for r in rows if cases[r["case_id"]]["category"] in INJECTION_CATEGORIES]
        others = [r for r in rows if cases[r["case_id"]]["category"] not in INJECTION_CATEGORIES]
        blocked = sum(1 for r in injection if r["metrics"]["safety"].get("blocked_links"))
        lines.append(f"| {label} | {stage} | {sum(map(passed, injection))}/{len(injection)} | {blocked} 次 "
                     f"| {sum(map(passed, others))}/{len(others)} |")
    return lines + [""]


def main() -> None:
    cases = dataset.load_all()
    lines = ["# 评测汇总", "", "各配置的完整报告见 `eval/results/<tag>/report.md`；指标定义见 `eval/README.md`。", ""]
    lines += config_table(cases)
    lines += stability_table(cases)
    lines += faults_table()
    lines += natural_failures()
    lines += safety_table(cases)
    lines += calibration_section()
    security = RESULTS_DIR / "security.md"
    if security.exists():
        lines += ["## 访问控制", ""] + security.read_text(encoding="utf-8").splitlines()[2:] + [""]
    path = RESULTS_DIR / "SUMMARY.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"-> {path}")


if __name__ == "__main__":
    main()
