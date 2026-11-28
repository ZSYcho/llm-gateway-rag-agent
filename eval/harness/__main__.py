"""Eval command line.

    python -m eval.harness run --tag baseline-routed --profile routed [--suite core,safety] [--reps 1]
                               [--set query_temperature=0] [--cases core-001,core-002] [--limit N]
    python -m eval.harness judge --tag baseline-routed [--model deepseek-v4-pro]
    python -m eval.harness report --tag baseline-routed
    python -m eval.harness compare --a baseline-routed --b temp0-routed
    python -m eval.harness retriever --tag baseline-routed      # retriever-only + threshold sweep
    python -m eval.harness replay --tag final-routed-v2         # recorded queries, other retrieval settings

Results go to eval/results/<tag>/: manifest.json, runs.jsonl, judged.jsonl,
report.md. Runs and judgements are appended as they finish, so an
interrupted command resumes where it stopped.
"""

import argparse
import hashlib
import json
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path

from eval.harness.paths import BACKEND_DIR, EVAL_DIR, ROOT

RESULTS_DIR = EVAL_DIR / "results"


def _jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _append(path: Path, record: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _parse_value(text: str):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _git(path: Path) -> str:
    sha = subprocess.run(["git", "-C", str(path), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(["git", "-C", str(path), "status", "--porcelain"], capture_output=True, text=True).stdout.strip()
    return sha + ("+dirty" if dirty else "")


def _manifest(args, cases: dict) -> dict:
    from agent import kb

    with kb.connect() as conn:
        docs = conn.execute("SELECT doc_path, content_hash, acl FROM kb_documents ORDER BY doc_path").fetchall()
    kb_fingerprint = hashlib.sha256(json.dumps(docs, default=str).encode()).hexdigest()[:12]
    prompts = (BACKEND_DIR / "src" / "agent" / "prompts.py").read_bytes()
    dataset = json.dumps([cases[c] for c in sorted(cases)], ensure_ascii=False, sort_keys=True).encode()
    try:
        with urllib.request.urlopen("http://localhost:11434/api/ps", timeout=5) as r:
            loaded = json.loads(r.read())["models"]
        ollama = {m["name"]: m.get("context_length") for m in loaded}
    except Exception:
        ollama = None
    return {
        "tag": args.tag,
        "profile": args.profile,
        "overrides": dict(args.set),
        "reps": args.reps,
        "suites": args.suite,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git": {
            "root": _git(ROOT),
            "agent": _git(ROOT / "repos" / "gemini-fullstack-langgraph-quickstart"),
            "gateway": _git(ROOT / "repos" / "new-api"),
        },
        "kb_fingerprint": kb_fingerprint,
        "kb_documents": len(docs),
        "prompts_sha": hashlib.sha256(prompts).hexdigest()[:12],
        "dataset_sha": hashlib.sha256(dataset).hexdigest()[:12],
        "ollama_loaded_context": ollama,
    }


def cmd_run(args) -> None:
    from eval.harness import dataset
    from eval.harness.runner import Runner

    cases = {cid: c for cid, c in dataset.load_all().items() if c["suite"] in args.suite}
    selected = [c for c in cases.values() if not args.cases or c["id"] in args.cases]
    if args.limit:
        selected = selected[: args.limit]
    out_dir = RESULTS_DIR / args.tag
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "manifest.json"
    if not manifest_path.exists():
        manifest_path.write_text(json.dumps(_manifest(args, cases), indent=2, ensure_ascii=False), encoding="utf-8")
    runs_path = out_dir / "runs.jsonl"
    done = {(r["case_id"], r["rep"]) for r in _jsonl(runs_path)}
    runner = Runner(args.tag, args.profile, dict(args.set), args.gateway_url)
    todo = [(c, rep) for rep in range(args.reps) for c in selected if (c["id"], rep) not in done]
    print(f"{args.tag}: {len(todo)} runs to do ({len(done)} already done)")
    for n, (case, rep) in enumerate(todo, 1):
        record = runner.run_case(case, rep)
        _append(runs_path, record)
        status = record["status"] if record["status"] == "ok" else f"{record['error']['type']}@{record['error']['stage']}"
        print(f"[{n}/{len(todo)}] {case['id']} r{rep} {status} {record['duration_s']}s", flush=True)


def cmd_judge(args) -> None:
    from eval.harness import dataset, judge

    cases = dataset.load_all()
    out_dir = RESULTS_DIR / args.tag
    judged_path = out_dir / "judged.jsonl"
    done = {(r["case_id"], r["rep"]) for r in _jsonl(judged_path)}
    runs = [r for r in _jsonl(out_dir / "runs.jsonl") if r["status"] == "ok" and (r["case_id"], r["rep"]) not in done]
    print(f"{args.tag}: judging {len(runs)} runs with {args.model}, {args.workers} at a time")

    def work(record):
        try:
            return record, judge.judge_record(cases[record["case_id"]], record, args.model), None
        except Exception as exc:  # reported, and retried on the next invocation
            return record, None, exc

    # Judge calls are network-bound, so they run concurrently; results are
    # written from this thread only.
    with ThreadPoolExecutor(args.workers) as pool:
        for n, future in enumerate(as_completed([pool.submit(work, r) for r in runs]), 1):
            record, result, exc = future.result()
            if exc is not None:
                print(f"[{n}/{len(runs)}] {record['case_id']} r{record['rep']} judge failed: {type(exc).__name__}: {exc}"[:300], flush=True)
                continue
            _append(judged_path, {"case_id": record["case_id"], "rep": record["rep"], **result})
            kp = result["key_points"]
            print(f"[{n}/{len(runs)}] {record['case_id']} r{record['rep']} correct={kp['correct']} coverage={kp['coverage']} "
                  f"faithfulness={result['faithfulness']['faithfulness']}", flush=True)
    _answer_similarity(out_dir)


def _answer_similarity(out_dir: Path) -> None:
    """Pairwise cosine similarity of the answers to the same case across repetitions."""
    import math

    from agent import kb

    runs = [r for r in _jsonl(out_dir / "runs.jsonl") if r["status"] == "ok"]
    by_case: dict[str, list[dict]] = {}
    for r in runs:
        by_case.setdefault(r["case_id"], []).append(r)
    repeated = {cid: rs for cid, rs in by_case.items() if len(rs) > 1}
    if not repeated:
        return
    path = out_dir / "stability.jsonl"
    done = {r["case_id"] for r in _jsonl(path)}
    embeddings = kb.get_embeddings("eval_stability")
    for cid, rs in repeated.items():
        if cid in done:
            continue
        vectors = embeddings.embed_documents([r["answer"] for r in rs])
        sims = [
            sum(x * y for x, y in zip(a, b)) / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))
            for a, b in combinations(vectors, 2)
        ]
        _append(path, {"case_id": cid, "answer_similarity": [round(s, 4) for s in sims]})


def _load_rows(tag: str, cases: dict) -> tuple[list[dict], dict]:
    from agent import kb
    from eval.harness import metrics

    out_dir = RESULTS_DIR / tag
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    judged = {(r["case_id"], r["rep"]): r for r in _jsonl(out_dir / "judged.jsonl")}
    stability = {r["case_id"]: r for r in _jsonl(out_dir / "stability.jsonl")}
    with kb.connect() as conn:
        acls = {row[0]: tuple(row[1]) for row in conn.execute("SELECT id, acl FROM kb_chunks").fetchall()}
    max_loops = int(manifest["overrides"].get("max_research_loops", 2))
    rows = []
    for record in _jsonl(out_dir / "runs.jsonl"):
        case = cases[record["case_id"]]
        record["metrics"] = metrics.compute(case, record, acls, max_loops)
        record["judge"] = judged.get((record["case_id"], record["rep"]))
        if record["rep"] == 0 and record["case_id"] in stability:
            record["stability"] = stability[record["case_id"]]
        rows.append(record)
    if judged:
        manifest["judge_model"] = next(iter(judged.values()))["judge_model"]
    return rows, manifest


def cmd_report(args) -> None:
    from eval.harness import dataset, report

    cases = dataset.load_all()
    rows, manifest = _load_rows(args.tag, cases)
    text = report.build(rows, cases, manifest)
    path = RESULTS_DIR / args.tag / "report.md"
    path.write_text(text, encoding="utf-8")
    print(text)
    print(f"\n-> {path.relative_to(ROOT)}")


def cmd_compare(args) -> None:
    from eval.harness import dataset, report

    cases = dataset.load_all()
    a_rows, _ = _load_rows(args.a, cases)
    b_rows, _ = _load_rows(args.b, cases)
    text = report.compare(a_rows, b_rows, cases, args.a, args.b)
    path = RESULTS_DIR / f"compare-{args.a}-vs-{args.b}.md"
    path.write_text(text, encoding="utf-8")
    print(text)
    print(f"\n-> {path.relative_to(ROOT)}")


def cmd_retriever(args) -> None:
    """Retriever-only metrics on the original question, and an offline threshold sweep."""
    from eval.harness import dataset
    from eval.harness.runner import IDENTITIES
    from agent import kb

    cases = dataset.load_all(("core",))
    lines = ["# 检索器单独评测", "", "直接用原始问题检索（不经过查询改写），k=10。", ""]
    recall5, recall10, mrr = [], [], []
    for case in cases.values():
        items = dataset.relevant_chunks(case)
        if not items:
            continue
        identity = IDENTITIES[case["identity"]]
        config = {"configurable": {"run_id": f"retriever.{args.tag}.{case['id']}", "langgraph_auth_user": identity}} if identity else {"configurable": {"run_id": f"retriever.{args.tag}.{case['id']}"}}
        ranked = [h["chunk_id"] for h in kb.search(case["turns"][-1], 10, config).hits]
        relevant = {cid for item in items for cid in item}
        recall5.append(sum(bool(set(i) & set(ranked[:5])) for i in items) / len(items))
        recall10.append(sum(bool(set(i) & set(ranked)) for i in items) / len(items))
        rank = next((n for n, cid in enumerate(ranked, 1) if cid in relevant), None)
        mrr.append(1 / rank if rank else 0.0)
    n = len(mrr)
    lines += ["| 指标 | 值 |", "|---|---|",
              f"| 题数 | {n} |",
              f"| Recall@5 | {sum(recall5) / n:.1%} |",
              f"| Recall@10 | {sum(recall10) / n:.1%} |",
              f"| MRR@10 | {sum(mrr) / n:.3f} |", ""]

    # Threshold sweep over the hits recorded in an agent run (no new retrievals).
    runs = [r for r in _jsonl(RESULTS_DIR / args.tag / "runs.jsonl") if r["status"] == "ok"]
    all_cases = dataset.load_all()
    if runs:
        lines += [f"## 相似度阈值扫描（基于 `{args.tag}` 记录的检索结果）", "",
                  "| 阈值 | 有答案题：证据召回 | 有答案题：平均通过片段数 | 无答案题：零片段通过的比例 |", "|---|---|---|---|"]
        for threshold in (0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70):
            recall, passed, empty = [], [], []
            for r in runs:
                case = all_cases[r["case_id"]]
                if case["suite"] == "safety":
                    continue
                hits = {h["chunk_id"] for e in r["turns"][-1]["retrieval_log"] for h in e["hits"] if h["score"] >= threshold}
                if case["answerable"]:
                    items = dataset.relevant_chunks(case)
                    if items:
                        recall.append(sum(bool(set(i) & hits) for i in items) / len(items))
                    passed.append(len(hits))
                else:
                    empty.append(not hits)
            fmt = lambda v: f"{sum(v) / len(v):.1%}" if v else "—"  # noqa: E731
            lines.append(f"| {threshold:.2f} | {fmt(recall)} | {sum(passed) / len(passed):.1f} | {fmt(empty)} |" if passed else f"| {threshold:.2f} | — | — | {fmt(empty)} |")
    path = RESULTS_DIR / args.tag / "retriever.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\n-> {path.relative_to(ROOT)}")


def cmd_replay(args) -> None:
    """Replay a run's recorded search queries with exact ranking at several k (no model calls but embeddings)."""
    from eval.harness import replay

    out_dir = RESULTS_DIR / args.tag
    runs = [r for r in _jsonl(out_dir / "runs.jsonl") if r["status"] == "ok"]
    judged = {(j["case_id"], j["rep"]): j["key_points"]["correct"] for j in _jsonl(out_dir / "judged.jsonl")}
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    if args.grid:
        text = replay.grid(args.tag, runs, manifest)
        path = out_dir / "replay-grid.md"
    else:
        text = replay.replay(args.tag, runs, judged, manifest, rerank=args.rerank)
        path = out_dir / "replay.md"
    path.write_text(text, encoding="utf-8")
    print(text)
    print(f"-> {path.relative_to(ROOT)}")


def cmd_faults(args) -> None:
    from eval.harness import chaos_proxy, dataset, faults
    from eval.harness.runner import Runner

    cases = dataset.load_all(("core",))
    out_dir = RESULTS_DIR / args.tag
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "manifest.json"
    if not manifest_path.exists():
        args.reps, args.suite = 1, ["core"]
        manifest_path.write_text(json.dumps(_manifest(args, cases), indent=2, ensure_ascii=False), encoding="utf-8")
    path = out_dir / "faults.jsonl"
    done = {(r["scenario"], r["case_id"]) for r in _jsonl(path)}
    try:
        server = chaos_proxy.start(faults.PROXY_PORT)
    except OSError:
        # A proxy is already serving the port (e.g. for the demo): drive that one.
        faults.injected()
        server = None
    runner = Runner(args.tag, args.profile, dict(args.set), faults.PROXY_URL + "/v1")
    try:
        for scenario in faults.SCENARIOS:
            for case_id in faults.CASES:
                if (scenario["id"], case_id) in done:
                    continue
                record = faults.run_scenario(runner, scenario, cases[case_id])
                _append(path, record)
                status = "ok" + (f" degraded={record['degraded']}" if record["degraded"] else "") if record["status"] == "ok" \
                    else f"{record['error']['type']}@{record['error']['stage']}"
                print(f"{scenario['id']:20} {case_id} {status} {record['duration_s']}s injected={record['injected']}", flush=True)
    finally:
        faults.set_rules({})
        if server is not None:
            server.shutdown()
    text = faults.summarize(_jsonl(path))
    (out_dir / "faults.md").write_text(text, encoding="utf-8")
    print(text)


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m eval.harness")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run")
    run.add_argument("--tag", required=True)
    run.add_argument("--profile", choices=("routed", "cloud", "local"), default="routed")
    run.add_argument("--suite", type=lambda s: s.split(","), default=["core", "robustness", "safety"])
    run.add_argument("--reps", type=int, default=1)
    run.add_argument("--set", action="append", default=[], type=lambda s: (s.split("=", 1)[0], _parse_value(s.split("=", 1)[1])),
                     help="configurable override, e.g. query_temperature=0")
    run.add_argument("--cases", type=lambda s: set(s.split(",")))
    run.add_argument("--limit", type=int)
    run.add_argument("--gateway-url", help="send model calls through this OpenAI-compatible URL (fault injection)")
    run.set_defaults(func=cmd_run)

    judge = sub.add_parser("judge")
    judge.add_argument("--tag", required=True)
    judge.add_argument("--model", default="deepseek-v4-pro")
    judge.add_argument("--workers", type=int, default=6)
    judge.set_defaults(func=cmd_judge)

    rep = sub.add_parser("report")
    rep.add_argument("--tag", required=True)
    rep.set_defaults(func=cmd_report)

    cmp = sub.add_parser("compare")
    cmp.add_argument("--a", required=True)
    cmp.add_argument("--b", required=True)
    cmp.set_defaults(func=cmd_compare)

    ret = sub.add_parser("retriever")
    ret.add_argument("--tag", required=True)
    ret.set_defaults(func=cmd_retriever)

    rpl = sub.add_parser("replay")
    rpl.add_argument("--tag", required=True)
    rpl.add_argument("--rerank", action="store_true", help="also score candidates with the reranker (kb.rerank)")
    rpl.add_argument("--grid", action="store_true", help="replay kb_retrieve for several candidate counts and context sizes")
    rpl.set_defaults(func=cmd_replay)

    flt = sub.add_parser("faults")
    flt.add_argument("--tag", required=True)
    flt.add_argument("--profile", choices=("routed", "cloud", "local"), default="routed")
    flt.add_argument("--set", action="append", default=[], type=lambda s: (s.split("=", 1)[0], _parse_value(s.split("=", 1)[1])))
    flt.set_defaults(func=cmd_faults)

    args = parser.parse_args()
    started = time.time()
    args.func(args)
    print(f"({time.time() - started:.0f}s)", file=sys.stderr)


if __name__ == "__main__":
    main()
