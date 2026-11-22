"""Eval cases and their gold evidence.

Evidence is labelled as a verbatim quote from a document, not as chunk ids:
a chunk is relevant to an evidence item when it belongs to that document and
contains the quote. Labels therefore survive re-chunking and re-ingestion,
which is what lets chunking strategies be compared on the same dataset.

    python -m eval.harness.dataset          # validate every suite
"""

import json
import re
import sys
from functools import lru_cache

from dotenv import load_dotenv

from eval.harness.paths import BACKEND_DIR, EVAL_DIR

sys.path.insert(0, str(BACKEND_DIR / "src"))
load_dotenv(BACKEND_DIR / ".env")

from agent import kb  # noqa: E402

DATASET_DIR = EVAL_DIR / "dataset"
SUITES = ("core", "robustness", "safety")
IDENTITY_GROUPS = {"anonymous": ["public"], "ops": ["ops", "public"], "redteam": ["public", "redteam"]}
REQUIRED = ("id", "category", "turns", "answerable")


def normalize(text: str) -> str:
    """Whitespace- and case-insensitive form used to match quotes against chunks."""
    return re.sub(r"\s+", " ", text).strip().lower()


def load(suite: str) -> list[dict]:
    cases = []
    with (DATASET_DIR / f"{suite}.jsonl").open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            if not line.strip():
                continue
            case = json.loads(line)
            missing = [key for key in REQUIRED if key not in case]
            if missing:
                raise ValueError(f"{suite}.jsonl:{line_no} {case.get('id')} missing {missing}")
            case["suite"] = suite
            case.setdefault("identity", "anonymous")
            case.setdefault("key_points", [])
            case.setdefault("must_not", [])
            case.setdefault("evidence", [])
            cases.append(case)
    ids = [c["id"] for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError(f"duplicate ids in {suite}.jsonl")
    return cases


def load_all(suites: tuple[str, ...] = SUITES) -> dict[str, dict]:
    """Cases by id; robustness variants inherit labels from their source case."""
    by_id: dict[str, dict] = {}
    for suite in suites:
        if (DATASET_DIR / f"{suite}.jsonl").exists():
            for case in load(suite):
                by_id[case["id"]] = case
    for case in by_id.values():
        source = by_id.get(case.get("variant_of", ""))
        if source:
            for key in ("answerable", "key_points", "must_not", "evidence", "identity"):
                if not case.get(key):
                    case[key] = source[key]
    return by_id


@lru_cache(maxsize=None)
def _doc_chunks(doc_path: str) -> tuple[tuple[int, str, tuple[str, ...]], ...]:
    with kb.connect() as conn:
        rows = conn.execute(
            "SELECT id, content, acl FROM kb_chunks WHERE doc_path = %s ORDER BY chunk_index",
            (doc_path,),
        ).fetchall()
    return tuple((row[0], normalize(row[1]), tuple(row[2])) for row in rows)


def evidence_chunks(item: dict) -> list[int]:
    """Ids of the chunks that contain one evidence quote."""
    quote = normalize(item["quote"])
    return [chunk_id for chunk_id, content, _ in _doc_chunks(item["doc"]) if quote in content]


def relevant_chunks(case: dict) -> list[list[int]]:
    """Per evidence item, the relevant chunk ids (an item may span chunk boundaries' copies)."""
    return [evidence_chunks(item) for item in case["evidence"]]


def validate(cases: dict[str, dict]) -> list[str]:
    problems = []
    for case in cases.values():
        if case["answerable"] and not case["key_points"]:
            problems.append(f"{case['id']}: answerable but no key_points")
        if case["answerable"] and not case["evidence"] and case["suite"] == "core":
            problems.append(f"{case['id']}: answerable core case without evidence")
        readable = set(IDENTITY_GROUPS[case["identity"]])
        for item in case["evidence"]:
            chunks = _doc_chunks(item["doc"])
            if not chunks:
                problems.append(f"{case['id']}: unknown doc {item['doc']}")
                continue
            matches = evidence_chunks(item)
            if not matches:
                problems.append(f"{case['id']}: quote not found in {item['doc']}: {item['quote'][:50]!r}")
            elif case["answerable"] and not any(set(acl) & readable for cid, _, acl in chunks if cid in matches):
                problems.append(f"{case['id']}: evidence not readable by identity {case['identity']}")
    return problems


def main() -> None:
    cases = load_all()
    problems = validate(cases)
    counts: dict[str, int] = {}
    for case in cases.values():
        key = f"{case['suite']}/{case['category']}"
        counts[key] = counts.get(key, 0) + 1
    for key, n in sorted(counts.items()):
        print(f"{n:4d}  {key}")
    print(f"{len(cases)} cases, {len(problems)} problems")
    for problem in problems:
        print("  -", problem)
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
