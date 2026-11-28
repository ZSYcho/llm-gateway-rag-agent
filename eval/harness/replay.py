"""Replay the search queries of a recorded eval run against other retrieval settings.

Rerunning the agent changes its queries and costs model calls. Replaying the
queries a run recorded isolates the retriever: same queries, same caller
groups, same evidence labels; only the retrieval differs. Ranking is exact
(a sequential scan ranks every chunk the caller may read with the agent's
distance operator), so HNSW approximation does not blur comparisons, and the
replay first checks that it reproduces the recorded hits.

With --rerank, the cosine candidates are also scored by the reranker
(agent.kb.rerank), to measure how much evidence a cross-encoder lifts into
the answer's context, what it costs in latency, and whether its scores can
tell answerable questions from unanswerable ones.

With --grid, the replay does exactly what kb_retrieve does now (threshold,
rerank against the user's recent turns, best chunks pooled over the queries)
for several candidate counts and context sizes, to pick the two values.

Follow-up queries were written after the reflection node had seen the
recorded hits, so a replay measures the retriever on fixed queries, not the
whole agent.

    python -m eval.harness replay --tag final-routed-v2 [--rerank | --grid]
"""

import hashlib
import json
import math
import statistics
import time
from dataclasses import dataclass

import psycopg
from pgvector import Vector

from eval.harness import dataset

# eval.harness.dataset puts the agent package on the path and loads its .env.
kb = dataset.kb

KS = (5, 10, 20, 50)
# Cosine candidates per query handed to the reranker. Cross-encoder scores
# are per (query, document) pair, so scoring the largest list covers all.
RERANK_CANDIDATES = (20, 50)
RERANK_THRESHOLDS = (0.01, 0.02, 0.05, 0.1, 0.2, 0.3, 0.5)
GRID_CANDIDATES = (20, 50)
GRID_CONTEXT = (6, 8, 10, 12)
# The agent before reranking: cosine top 5 per query, best 12 in the context.
BASELINE_TOP_K, BASELINE_CONTEXT = 5, 12

Ranking = list[tuple[int, float]]


def exact_ranking(conn: psycopg.Connection, vector: list[float], groups: list[str]) -> Ranking:
    """Every chunk the groups may read, most similar first, as (chunk id, cosine similarity)."""
    with conn.transaction():
        # Without index scans the planner ranks every row: exact, unlike HNSW.
        conn.execute("SET LOCAL enable_indexscan = off")
        rows = conn.execute(
            """
            SELECT id, 1 - (embedding <=> %(v)s) AS score
            FROM kb_chunks
            WHERE acl && %(groups)s
            ORDER BY embedding <=> %(v)s, id
            """,
            {"v": Vector(vector), "groups": groups},
        ).fetchall()
    return [(row[0], float(row[1])) for row in rows]


def chunk_texts(conn: psycopg.Connection) -> dict[int, str]:
    """The text the reranker reads for each chunk: the same heading path + content that is embedded."""
    rows = conn.execute(
        "SELECT c.id, d.title, c.heading, c.content FROM kb_chunks c JOIN kb_documents d USING (doc_path)"
    ).fetchall()
    return {row[0]: kb.chunk_embedding_text(row[1], row[2], row[3]) for row in rows}


def kb_fingerprint() -> str:
    """Same fingerprint as an eval run's manifest, to tell whether the knowledge base changed."""
    with kb.connect() as conn:
        docs = conn.execute("SELECT doc_path, content_hash, acl FROM kb_documents ORDER BY doc_path").fetchall()
    return hashlib.sha256(json.dumps(docs, default=str).encode()).hexdigest()[:12]


def _coverage(items: list[list[int]], ids: set[int]) -> float:
    return sum(1 for item in items if set(item) & ids) / len(items)


def _best(scored: list[tuple[int, float]]) -> dict[int, float]:
    """Highest score of each chunk over several queries' results."""
    best: dict[int, float] = {}
    for chunk_id, score in scored:
        if score > best.get(chunk_id, -math.inf):
            best[chunk_id] = score
    return best


def _top(scores: dict[int, float], limit: int) -> list[int]:
    return sorted(scores, key=scores.get, reverse=True)[:limit]


def _pct(values) -> str:
    values = list(values)
    return f"{sum(values) / len(values):.1%}" if values else "—"


def _quantiles(values: list[float]) -> str:
    values = sorted(values)
    return f"{statistics.median(values):.0f} / {values[int(0.95 * (len(values) - 1))]:.0f}"


def key(entry: dict) -> tuple[str, tuple[str, ...]]:
    """A retrieval is determined by its query and the caller's groups."""
    return entry["query"], tuple(entry["groups"])


@dataclass
class Prepared:
    """A recorded run's retrievals, ranked exactly, ready to be replayed."""

    defaults: object  # agent.configuration.Configuration
    plan: list[tuple[dict, dict, list[dict]]]  # (run, case, the run's successful retrievals)
    queries: list[str]
    rankings: dict[tuple[str, tuple[str, ...]], Ranking]
    agreement: list[float]  # per retrieval: share of the recorded hits the replay reproduces
    texts: dict[int, str]  # what the reranker reads per chunk (only when needed)


def prepare(runs: list[dict], with_texts: bool) -> Prepared:
    from agent.configuration import Configuration

    cases = dataset.load_all()
    plan = []
    for run in runs:
        entries = [entry for entry in run["turns"][-1]["retrieval_log"] if not entry.get("error")]
        if entries:
            plan.append((run, cases[run["case_id"]], entries))
    embeddings = kb.get_embeddings("retrieval_replay")
    queries = sorted({entry["query"] for _, _, entries in plan for entry in entries})
    vectors = {query: embeddings.embed_query(query) for query in queries}
    rankings: dict[tuple[str, tuple[str, ...]], Ranking] = {}
    with kb.connect() as conn:
        for _, _, entries in plan:
            for entry in entries:
                if key(entry) not in rankings:
                    rankings[key(entry)] = exact_ranking(conn, vectors[entry["query"]], entry["groups"])
        texts = chunk_texts(conn) if with_texts else {}
    agreement = [
        len({hit["chunk_id"] for hit in entry["hits"]} & {cid for cid, _ in rankings[key(entry)][: len(entry["hits"])]})
        / len(entry["hits"])
        for _, _, entries in plan
        for entry in entries
        if entry["hits"]
    ]
    return Prepared(Configuration(), plan, queries, rankings, agreement, texts)


def _header(tag: str, prepared: Prepared, labelled: int, manifest: dict) -> list[str]:
    fingerprint = kb_fingerprint()
    same_kb = "一致" if fingerprint == manifest.get("kb_fingerprint") else "**不一致，结果不可直接比较**"
    return [
        (
            f"把 `{tag}` 记录的 {len(prepared.agreement)} 次检索（{len(prepared.queries)} 个不同的检索词）重新检索一遍，"
            f"按余弦相似度对调用方可读的全部片段精确排序。召回指标覆盖 {labelled} 次带证据标注的运行。"
        ),
        "",
        f"- 知识库指纹：当前 `{fingerprint}`，评测时 `{manifest.get('kb_fingerprint')}`，{same_kb}",
        f"- 复现检查：回放的检索结果与记录的命中一致 {_pct(prepared.agreement)}",
    ]


def grid(tag: str, runs: list[dict], manifest: dict) -> str:
    """Candidate counts x context sizes, replaying exactly what kb_retrieve does now."""
    from agent.utils import rerank_query
    from langchain_core.messages import HumanMessage

    prepared = prepare(runs, with_texts=True)
    rankings, texts = prepared.rankings, prepared.texts
    min_score = prepared.defaults.retrieval_min_score
    # Reranker scores per (run, retrieval) for each candidate count, with the
    # time of each call: one call per retrieval, as in kb_retrieve.
    scores: dict[int, dict[tuple[int, int], dict[int, float]]] = {n: {} for n in GRID_CANDIDATES}
    call_ms: dict[int, list[float]] = {n: [] for n in GRID_CANDIDATES}
    run_ms: dict[int, list[float]] = {n: [] for n in GRID_CANDIDATES}
    for n in GRID_CANDIDATES:
        for i, (run, _, entries) in enumerate(prepared.plan):
            question = rerank_query([HumanMessage(turn["question"]) for turn in run["turns"]])
            total = 0.0
            for j, entry in enumerate(entries):
                ids = [cid for cid, s in rankings[key(entry)][:n] if s >= min_score]
                scores[n][(i, j)] = {}
                if not ids:
                    continue
                started = time.perf_counter()
                relevance = kb.rerank(question, [texts[cid] for cid in ids], node="retrieval_replay")
                elapsed = (time.perf_counter() - started) * 1000
                call_ms[n].append(elapsed)
                total += elapsed
                scores[n][(i, j)] = dict(zip(ids, relevance))
            run_ms[n].append(total)

    rows = []
    for i, (_, case, entries) in enumerate(prepared.plan):
        items = dataset.relevant_chunks(case)
        if not items:
            continue
        before = _best([
            (cid, s) for entry in entries for cid, s in rankings[key(entry)][:BASELINE_TOP_K] if s >= min_score
        ])
        row = {"items": items, "before": _top(before, BASELINE_CONTEXT)}
        for n in GRID_CANDIDATES:
            # Every chunk is scored against the same question, so pooling all
            # candidates equals pooling each retrieval's best few, as the agent does.
            pooled = _best([(cid, s) for j in range(len(entries)) for cid, s in scores[n][(i, j)].items()])
            for c in GRID_CONTEXT:
                row[(n, c)] = _top(pooled, c)
        rows.append(row)

    def stats(label: str, n: str, contexts: list[list[int]], ms: str) -> str:
        relevant = [{cid for item in r["items"] for cid in item} for r in rows]
        chars = sum(sum(len(texts.get(cid, "")) for cid in ctx) for ctx in contexts) / len(contexts)
        return (
            f"| {label} | {n} | {_pct(_coverage(r['items'], set(ctx)) for r, ctx in zip(rows, contexts))} "
            f"| {_pct(bool(rel & set(ctx)) for rel, ctx in zip(relevant, contexts))} | {chars:,.0f} | {ms} |"
        )

    out = [f"# 检索回放：候选数 × 上下文条数（{tag}）", ""]
    out += _header(tag, prepared, len(rows), manifest)
    out += [
        (
            f"- 与 Agent 现在的做法相同：每个检索词取前 N 条候选，相似度 ≥ {min_score} 才交给重排，"
            "按用户最近几轮的提问打分，汇总所有检索词的候选后取前 C 条进上下文"
        ),
        "- 上下文字数：进入上下文的片段文本（标题路径 + 正文）总字数，回答提示词的长度和费用大致与它成正比",
        "",
        "| 候选数 N | 上下文条数 C | 上下文证据召回 | 上下文至少一条证据 | 平均上下文字数 | 每题重排耗时 P50 / P95（ms） |",
        "|---|---|---|---|---|---|",
        stats(f"重排前（每个检索词前 {BASELINE_TOP_K} 条，按相似度）", str(BASELINE_CONTEXT),
              [r["before"] for r in rows], "—"),
    ]
    for n in GRID_CANDIDATES:
        for c in GRID_CONTEXT:
            out.append(stats(str(n), str(c), [r[(n, c)] for r in rows], _quantiles(run_ms[n])))
    out += [
        "",
        "每次重排（一个检索词的候选）耗时 P50 / P95："
        + "；".join(f"{n} 条候选 {_quantiles(call_ms[n])} ms" for n in GRID_CANDIDATES)
        + "。每题耗时是该题所有检索词的重排耗时之和；Agent 里同一轮的检索并行，实际等待会短一些，但它们共用一块显卡。",
    ]
    return "\n".join(out) + "\n"


def replay(tag: str, runs: list[dict], judged: dict[tuple[str, int], bool], manifest: dict, rerank: bool = False) -> str:
    """Markdown report; ``judged`` maps (case id, rep) to whether the answer was correct."""
    prepared = prepare(runs, with_texts=rerank)
    defaults, plan = prepared.defaults, prepared.plan
    rankings, texts = prepared.rankings, prepared.texts

    # Reranker scores: per query for its own candidates, and per run for the
    # pooled candidates against the user's question.
    n_max = max(RERANK_CANDIDATES)
    pair_scores: dict[tuple[str, tuple[str, ...]], dict[int, float]] = {}
    question_scores: dict[int, dict[int, float]] = {}
    call_ms: list[float] = []
    if rerank:
        for query_key, ranking in rankings.items():
            ids = [chunk_id for chunk_id, _ in ranking[:n_max]]
            started = time.perf_counter()
            scores = kb.rerank(query_key[0], [texts[chunk_id] for chunk_id in ids], node="retrieval_replay")
            call_ms.append((time.perf_counter() - started) * 1000)
            pair_scores[query_key] = dict(zip(ids, scores))
        for index, (run, _, entries) in enumerate(plan):
            pool = list(dict.fromkeys(cid for entry in entries for cid, _ in rankings[key(entry)][:n_max]))
            scores = kb.rerank(run["turns"][-1]["question"], [texts[cid] for cid in pool], node="retrieval_replay")
            question_scores[index] = dict(zip(pool, scores))
        # Some servers return raw logits; map them to 0-1 so thresholds read as probabilities.
        everything = [s for d in (*pair_scores.values(), *question_scores.values()) for s in d.values()]
        if any(s < 0 or s > 1 for s in everything):

            def squash(s: float) -> float:
                return 1 / (1 + math.exp(-s))

            pair_scores = {k: {c: squash(s) for c, s in d.items()} for k, d in pair_scores.items()}
            question_scores = {k: {c: squash(s) for c, s in d.items()} for k, d in question_scores.items()}

    def reranked(entries: list[dict], n: int) -> dict[int, float]:
        """Each query's top-n cosine candidates scored against that query; best score per chunk."""
        return _best([
            (cid, pair_scores[key(entry)][cid]) for entry in entries for cid, _ in rankings[key(entry)][:n]
        ])

    rows: list[dict] = []
    for index, (run, case, entries) in enumerate(plan):
        items = dataset.relevant_chunks(case)
        per_query = [rankings[key(entry)] for entry in entries]
        row = {"case_id": run["case_id"], "answerable": case["answerable"], "items": items,
               "correct": judged.get((run["case_id"], run["rep"]))}
        relevant = {chunk_id for item in items for chunk_id in item}
        row["best"] = min(
            (
                (rank, score, entry["query"])
                for entry, ranking in zip(entries, per_query)
                for rank, (chunk_id, score) in enumerate(ranking, 1)
                if chunk_id in relevant
            ),
            default=(None, None, None),
        )
        for k in KS:
            pool = {chunk_id for ranking in per_query for chunk_id, _ in ranking[:k]}
            cosine = _best([(cid, s) for ranking in per_query for cid, s in ranking[:k] if s >= defaults.retrieval_min_score])
            context = set(_top(cosine, defaults.max_context_chunks))
            row[k] = {"pool": pool, "context": context}
        if rerank:
            for n in RERANK_CANDIDATES:
                row[f"rerank@{n}"] = reranked(entries, n)
                pool = {cid for ranking in per_query for cid, _ in ranking[:n]}
                row[f"question@{n}"] = {cid: s for cid, s in question_scores[index].items() if cid in pool}
            row["rerank_pool"] = {cid for ranking in per_query for cid, _ in ranking[:n_max]}
            order = _top(row[f"rerank@{n_max}"], len(row[f"rerank@{n_max}"]))
            row["rerank_rank"] = next((rank for rank, cid in enumerate(order, 1) if cid in relevant), None)
        rows.append(row)

    labelled = [r for r in rows if r["items"]]
    out = [f"# 检索回放：{tag}", ""]
    out += _header(tag, prepared, len(labelled), manifest)
    out += [
        (
            f"- 进入上下文的规则与重排前的 Agent 相同：相似度 ≥ {defaults.retrieval_min_score}，"
            f"去重后按相似度取前 {defaults.max_context_chunks} 条"
        ),
        "",
        "## 加大 k",
        "",
        "| 每个检索词取前 k 条 | 候选池证据召回 | 候选池至少一条证据 | 平均候选数 | 进入上下文的证据召回 |",
        "|---|---|---|---|---|",
    ]
    for k in KS:
        label = f"{k}（重排前）" if k == BASELINE_TOP_K else str(k)
        out.append(
            f"| {label} | {_pct(_coverage(r['items'], r[k]['pool']) for r in labelled)} "
            f"| {_pct(bool(r[k]['pool'] & {c for i in r['items'] for c in i}) for r in labelled)} "
            f"| {sum(len(r[k]['pool']) for r in labelled) / len(labelled):.1f} "
            f"| {_pct(_coverage(r['items'], r[k]['context']) for r in labelled)} |"
        )

    if rerank:
        limit = defaults.max_context_chunks
        k_now = BASELINE_TOP_K

        def context_row(label: str, contexts: list[set[int]]) -> str:
            hits = [bool(ctx & {c for i in r["items"] for c in i}) for r, ctx in zip(labelled, contexts)]
            return f"| {label} | {_pct(_coverage(r['items'], ctx) for r, ctx in zip(labelled, contexts))} | {_pct(hits)} |"

        ms_by_query = dict(zip(rankings, call_ms))  # call_ms was filled in the order of rankings
        per_run_ms = [sum(ms_by_query[key(entry)] for entry in entries) for _, _, entries in plan]

        def passed(r: dict, threshold: float) -> list[int]:
            scores = r[f"rerank@{n_max}"]
            return [cid for cid in _top(scores, limit) if scores[cid] >= threshold]

        out += [
            "",
            f"## 重排：上下文仍取 {limit} 条",
            "",
            f"重排模型 `{kb.RERANK_MODEL}`。片段文本与入库向量化时相同（标题路径 + 正文）。",
            "",
            "| 方案 | 进入上下文的证据召回 | 上下文至少一条证据 |",
            "|---|---|---|",
            context_row(f"重排前：每个检索词取前 {k_now} 条，按相似度", [r[k_now]["context"] for r in labelled]),
        ]
        for n in RERANK_CANDIDATES:
            out.append(context_row(f"每个检索词取前 {n} 条，按相似度", [r[n]["context"] for r in labelled]))
        for n in RERANK_CANDIDATES:
            out.append(context_row(f"每个检索词取前 {n} 条，各自按检索词重排",
                                   [set(_top(r[f"rerank@{n}"], limit)) for r in labelled]))
        for n in RERANK_CANDIDATES:
            out.append(context_row(f"每个检索词取前 {n} 条，汇总后按用户问题重排",
                                   [set(_top(r[f"question@{n}"], limit)) for r in labelled]))
        out.append(context_row(f"上限：前 {n_max} 条候选池里有就算", [r["rerank_pool"] for r in labelled]))
        out += [
            "",
            (
                f"耗时：每次重排 {n_max} 条候选 P50 / P95 = {_quantiles(call_ms)} ms；"
                f"每次运行（所有检索词相加）P50 / P95 = {_quantiles(per_run_ms)} ms。"
            ),
            "",
            f"## 重排分数阈值（每个检索词前 {n_max} 条，各自按检索词重排）",
            "",
            f"分数已归一到 0~1。现在的余弦阈值 {defaults.retrieval_min_score} 对照见 `retriever.md`。",
            "",
            "| 阈值 | 有答案题：上下文证据召回 | 有答案题：平均通过片段数 | 有答案题：零片段通过的比例 | 无答案题：零片段通过的比例 |",
            "|---|---|---|---|---|",
        ]
        answerable = [r for r in rows if r["answerable"]]
        unanswerable = [r for r in rows if not r["answerable"]]
        for t in RERANK_THRESHOLDS:
            out.append(
                f"| {t} | {_pct(_coverage(r['items'], set(passed(r, t))) for r in answerable if r['items'])} "
                f"| {sum(len(passed(r, t)) for r in answerable) / len(answerable):.1f} "
                f"| {_pct(not passed(r, t) for r in answerable)} | {_pct(not passed(r, t) for r in unanswerable)} |"
            )

    misses = [r for r in labelled if not _coverage(r["items"], r[BASELINE_TOP_K]["pool"])]
    out += [
        "",
        f"## 取前 {BASELINE_TOP_K} 条时一条证据都没找到的运行（{len(misses)} 次）",
        "",
        "余弦排名是在调用方可读的全部片段里的名次，取这次运行所有检索词里最好的一个。",
        "",
        (
            "| 用例 | 回答判定 | 余弦最好排名 | 该片段相似度 | 取得这个排名的检索词 |"
            + (f" 重排后排名（前 {n_max} 条候选内） |" if rerank else "")
        ),
        "|---|---|---|---|---|" + ("---|" if rerank else ""),
    ]
    for r in sorted(misses, key=lambda r: r["best"][0] or 10**9):
        rank, score, query = r["best"]
        verdict = {True: "对", False: "错", None: "—"}[r["correct"]]
        line = (
            f"| {r['case_id']} | {verdict} | {rank or '—'} | {f'{score:.3f}' if score is not None else '—'} "
            f"| {query or '—'} |"
        )
        if rerank:
            line += f" {r['rerank_rank'] or '—'} |"
        out.append(line)
    return "\n".join(out) + "\n"
