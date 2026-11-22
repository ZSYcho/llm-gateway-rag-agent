"""Model-graded metrics: key-point coverage, refusal, and claim-level faithfulness.

The judge is a stronger model than the one under test (deepseek-v4-pro by
default) so the answer model does not grade itself. It makes only binary or
three-way decisions per key point and per claim, which are more reliable than
holistic scores, and its calls are tagged ``eval_judge`` at the gateway so
their cost is kept apart from the agent's.
"""

import os
import re
from typing import Literal

from langchain_core.exceptions import OutputParserException
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field, ValidationError

from eval.harness import runner  # noqa: F401  (loads the agent's .env and import path)
from agent import kb

JUDGE_MODEL = "deepseek-v4-pro"


class PointVerdict(BaseModel):
    index: int = Field(description="Number of the key point, starting at 1.")
    verdict: Literal["yes", "partial", "no"]
    reason: str = Field(description="One short sentence.")


class KeyPointJudgement(BaseModel):
    points: list[PointVerdict] = Field(description="One verdict per key point, in order.")
    violated_must_not: list[int] = Field(description="Numbers of the must-not statements the answer makes.")
    response_type: Literal["answered", "partially_refused", "refused"] = Field(
        description="refused: says the documentation does not cover the question and gives no substantive answer; "
        "partially_refused: answers part and says the rest is not covered; answered: otherwise."
    )


class Claim(BaseModel):
    claim: str
    kind: Literal["fact", "not_covered"] = Field(
        default="fact",
        description="not_covered: the claim only says that the documentation or excerpts do not "
        "contain or cover something; fact: any other claim."
    )
    verdict: Literal["supported", "unsupported", "contradicted"]
    support_ids: list[int] = Field(description="Ids of excerpts that support the claim.")
    cited_ids: list[int] = Field(description="Ids in the [C...] citations attached to the sentence of this claim.")


class FaithfulnessJudgement(BaseModel):
    claims: list[Claim]


KEY_POINTS_PROMPT = """You are grading an answer written by a documentation assistant. The answer may be in Chinese or English.

Question:
{question}

Answer:
{answer}

Key points a correct answer must contain:
{key_points}

Statements the answer must not make:
{must_not}

For each key point, decide "yes" if the answer clearly states it (paraphrases and other languages count), "partial" if it is only implied, mentioned without its key detail, or vague, and "no" if it is missing or stated wrongly. Grade only against the key points; do not add requirements from your own knowledge. Then list the must-not statements the answer makes, and classify the response type."""


FAITHFULNESS_PROMPT = """You are checking whether an answer is grounded in the excerpts it was written from. The answer may be in Chinese or English. Citations in the answer appear as markdown links whose target is [C<id>]; plain-text labels without a link were citations to excerpts that do not exist.

Excerpts:
{excerpts}

Answer:
{answer}

Split the answer into atomic claims, skipping greetings and hedges. Mark a claim as kind "not_covered" when it only says that the documentation or excerpts do not contain something; mark every other claim as kind "fact". For each claim decide:
- supported: an excerpt states it or it follows directly from the excerpts;
- unsupported: no excerpt says it (even if it is true);
- contradicted: an excerpt says otherwise.
Give the ids of supporting excerpts, and the ids cited by the sentence that contains the claim. If the answer makes no claims at all, return an empty list."""


def judge_model(model: str, tag: str, case_id: str, rep: int) -> ChatOpenAI:
    extra_body = {"thinking": {"type": "disabled"}} if model.startswith("deepseek") else None
    return ChatOpenAI(
        model=model,
        base_url=os.getenv("GATEWAY_BASE_URL", "http://localhost:3000/v1"),
        api_key=os.environ["GATEWAY_API_KEY"],
        temperature=0,
        timeout=180,
        max_retries=2,
        extra_body=extra_body,
        default_headers={
            "X-Agent-Run-Id": f"judge.{tag}.{case_id}.r{rep}"[:128],
            "X-Agent-Node": "eval_judge",
        },
    )


def _structured(llm: ChatOpenAI, schema: type[BaseModel], prompt: str, attempts: int = 3) -> BaseModel:
    """Function-calling output, retried when the model answers without calling the function."""
    structured = llm.with_structured_output(schema, method="function_calling")
    last_error: Exception | None = None
    for _ in range(attempts):
        try:
            result = structured.invoke(prompt)
        except (ValidationError, OutputParserException) as exc:
            last_error = exc
            continue
        if result is not None:
            return result
    raise ValueError(f"judge returned no parsable {schema.__name__} after {attempts} attempts: {last_error}")


def _numbered(items: list[str]) -> str:
    return "\n".join(f"{i}. {item}" for i, item in enumerate(items, 1)) or "(none)"


def cited_answer(answer: str, citations: list[dict]) -> str:
    """Point each resolved citation link back at its chunk id, in order of appearance."""
    position = 0
    for citation in citations:
        if not citation["valid"]:
            continue
        match = re.compile(re.escape(f"[{citation['label']}](") + r"[^)]*\)").search(answer, position)
        if match is None:
            continue
        replacement = f"[{citation['label']}]([C{citation['chunk_id']}])"
        answer = answer[: match.start()] + replacement + answer[match.end():]
        position = match.start() + len(replacement)
    return answer


def chunk_excerpts(chunk_ids: list[int]) -> str:
    if not chunk_ids:
        return "(no excerpts)"
    with kb.connect() as conn:
        rows = conn.execute(
            """SELECT c.id, d.title, c.heading, c.content
               FROM kb_chunks c JOIN kb_documents d USING (doc_path) WHERE c.id = ANY(%s)""",
            (chunk_ids,),
        ).fetchall()
    by_id = {row[0]: row for row in rows}
    parts = []
    for chunk_id in chunk_ids:
        if chunk_id in by_id:
            _, title, heading, content = by_id[chunk_id]
            parts.append(f"[C{chunk_id}] {title} > {heading}\n{content}")
    return "\n\n---\n\n".join(parts)


def judge_record(case: dict, record: dict, model: str = JUDGE_MODEL) -> dict:
    turn = record["turns"][-1]
    llm = judge_model(model, record["tag"], record["case_id"], record["rep"])
    result: dict = {"judge_model": model}

    key_points = _structured(llm, KeyPointJudgement,
        KEY_POINTS_PROMPT.format(
            question=turn["question"],
            answer=turn["answer"],
            key_points=_numbered(case["key_points"]),
            must_not=_numbered(case["must_not"]),
        )
    )
    scores = {"yes": 1.0, "partial": 0.5, "no": 0.0}
    verdicts = [p.verdict for p in sorted(key_points.points, key=lambda p: p.index)][: len(case["key_points"])]
    coverage = sum(scores[v] for v in verdicts) / len(case["key_points"]) if case["key_points"] else None
    refused = key_points.response_type == "refused"
    if case["answerable"]:
        correct = coverage is not None and coverage >= 0.8 and not key_points.violated_must_not and not refused
    else:
        correct = refused and not key_points.violated_must_not
    result["key_points"] = {
        **key_points.model_dump(),
        "coverage": coverage,
        "correct": correct,
        "over_refusal": case["answerable"] and refused,
        "hallucinated_unanswerable": (not case["answerable"]) and key_points.response_type != "refused",
    }

    try:
        claims = _structured(llm, FaithfulnessJudgement,
            FAITHFULNESS_PROMPT.format(
                excerpts=chunk_excerpts(turn["context_chunk_ids"]),
                answer=cited_answer(turn["answer"], turn["citations"]),
            )
        ).claims
    except ValueError as exc:
        # Keep the key-point verdict; faithfulness is reported as missing.
        result["faithfulness"] = {"error": str(exc)[:200], "claims": [], "n_claims": 0, "faithfulness": None,
                                  "contradicted": 0, "citation_coverage": None, "citation_precision": None}
        return result
    # Statements that something is not covered are refusals, not facts to ground.
    claims = [c for c in claims if c.kind == "fact"]
    total = len(claims)
    cited = [c for c in claims if c.cited_ids]
    result["faithfulness"] = {
        "claims": [c.model_dump() for c in claims],
        "n_claims": total,
        "faithfulness": sum(c.verdict == "supported" for c in claims) / total if total else None,
        "contradicted": sum(c.verdict == "contradicted" for c in claims),
        "citation_coverage": len(cited) / total if total else None,
        "citation_precision": sum(bool(set(c.cited_ids) & set(c.support_ids)) for c in cited) / len(cited) if cited else None,
    }
    return result
