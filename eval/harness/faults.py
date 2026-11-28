"""Fault-injection scenarios: one failing stage at a time, through the chaos proxy.

    python -m eval.harness faults --tag faults-baseline [--set llm_timeout_seconds=10]

Each scenario runs the same few cases. The question per scenario is whether
the run still produces an answer (possibly degraded), fails with a clear
error, or hangs; and how long it takes either way.
"""

import json
import os
import statistics
import urllib.request
from collections import Counter

from eval.harness.errors import failure_type

PROXY_PORT = 3999
PROXY_URL = f"http://127.0.0.1:{PROXY_PORT}"
CASES = ["core-001", "core-025", "core-047"]
SCENARIOS = [
    {"id": "none", "label": "无故障（对照）", "rules": {}},
    # The cloud answer model fails; other models behind the gateway still work.
    {"id": "answer-5xx", "label": "云端回答模型上游 500", "rules": {"finalize_answer": {"fault": "status", "status": 500, "model": "deepseek-flash"}}},
    {"id": "answer-429", "label": "云端回答模型被限流 429", "rules": {"finalize_answer": {"fault": "status", "status": 429, "model": "deepseek-flash"}}},
    {"id": "answer-402", "label": "云端账户余额不足 402", "rules": {"finalize_answer": {"fault": "status", "status": 402, "model": "deepseek-flash"}}},
    {"id": "embed-503", "label": "向量化接口 503", "rules": {"kb_retrieve": {"fault": "status", "status": 503}}},
    # Only the reranker fails; retrieval itself works and falls back to cosine order.
    {"id": "rerank-503", "label": "重排接口 503", "rules": {"kb_retrieve": {"fault": "status", "status": 503, "model": "bge-reranker-v2-m3"}}},
    {"id": "query-drop", "label": "查询生成连接中断", "rules": {"generate_query": {"fault": "drop"}}},
    {"id": "reflection-garbage", "label": "反思输出无法解析", "rules": {"reflection": {"fault": "garbage"}}},
    {"id": "reflection-slow", "label": "反思节点慢 25 秒", "rules": {"reflection": {"fault": "delay", "seconds": 25}}},
    {"id": "flaky-30pct", "label": "所有调用 30% 概率 500", "rules": {"*": {"fault": "status", "status": 500, "p": 0.3}}},
    {"id": "db-down", "label": "向量库连不上", "rules": {}, "env": {"KB_DATABASE_URL": "postgresql://root:devpass123@localhost:5999/kb"}},
]


def set_rules(rules: dict) -> None:
    request = urllib.request.Request(f"{PROXY_URL}/__chaos", json.dumps({"rules": rules}).encode(),
                                     {"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(request, timeout=10).read()


def injected() -> dict:
    with urllib.request.urlopen(f"{PROXY_URL}/__chaos", timeout=10) as response:
        return json.loads(response.read())["injected"]


def run_scenario(runner, scenario: dict, case: dict) -> dict:
    saved = {key: os.environ.get(key) for key in scenario.get("env", {})}
    os.environ.update(scenario.get("env", {}))
    set_rules(scenario["rules"])
    try:
        record = runner.run_case(case, rep=0)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    record["scenario"] = scenario["id"]
    record["injected"] = injected()
    turn = record["turns"][-1]
    record["degraded"] = turn.get("degraded") or []
    return record


def summarize(records: list[dict]) -> str:
    lines = ["## 故障注入", "",
             "| 场景 | 运行 | 成功 | 其中降级回答 | 失败（类型@阶段） | 平均耗时 | 注入次数 |",
             "|---|---|---|---|---|---|---|"]
    labels = {s["id"]: s["label"] for s in SCENARIOS}
    for scenario in SCENARIOS:
        rs = [r for r in records if r["scenario"] == scenario["id"]]
        if not rs:
            continue
        ok = [r for r in rs if r["status"] == "ok"]
        degraded = [r for r in ok if r["degraded"]]
        failures = Counter(f"{failure_type(r['error'])}@{r['error']['stage']}" for r in rs if r["status"] != "ok")
        injected_total = sum(sum(r["injected"].values()) for r in rs)
        duration = statistics.fmean(r["duration_s"] for r in rs)
        lines.append(f"| {labels[scenario['id']]} | {len(rs)} | {len(ok)} | {len(degraded)} | {dict(failures) or '—'} | {duration:.1f}s | {injected_total} |")
    return "\n".join(lines) + "\n"
