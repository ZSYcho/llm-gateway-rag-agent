"""Run tracer: node spans and model calls of one graph run, from LangChain callbacks.

Graph nodes run in a thread pool when they fan out, so every callback takes
the lock. Model calls are attributed to nodes through the ``langgraph_node``
metadata that LangGraph puts on every run inside a node.
"""

import threading
import time
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler

NODES = ("generate_query", "kb_retrieve", "reflection", "finalize_answer")


def _now_ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 1)


class RunTracer(BaseCallbackHandler):
    raise_error = False

    def __init__(self) -> None:
        self.t0 = time.perf_counter()
        self.lock = threading.Lock()
        self.root: UUID | None = None
        self.spans: dict[UUID, dict] = {}
        self.calls: dict[UUID, dict] = {}

    # ------------------------------------------------------------- node spans

    def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, metadata=None, **kwargs):
        with self.lock:
            if self.root is None and parent_run_id is None:
                self.root = run_id
                return
            name = kwargs.get("name")
            if parent_run_id == self.root and name in NODES:
                self.spans[run_id] = {
                    "node": name,
                    "step": (metadata or {}).get("langgraph_step"),
                    "start_ms": _now_ms(self.t0),
                    "end_ms": None,
                    "output": None,
                    "error": None,
                }

    def on_chain_end(self, outputs, *, run_id, **kwargs):
        with self.lock:
            span = self.spans.get(run_id)
            if span is not None:
                span["end_ms"] = _now_ms(self.t0)
                span["output"] = _summarize_output(span["node"], outputs)

    def on_chain_error(self, error, *, run_id, **kwargs):
        with self.lock:
            span = self.spans.get(run_id)
            if span is not None:
                span["end_ms"] = _now_ms(self.t0)
                span["error"] = f"{type(error).__name__}: {error}"[:500]

    # ------------------------------------------------------------ model calls

    def on_chat_model_start(self, serialized, messages, *, run_id, metadata=None, invocation_params=None, **kwargs):
        metadata = metadata or {}
        params = invocation_params or {}
        with self.lock:
            self.calls[run_id] = {
                "node": metadata.get("langgraph_node"),
                "model": params.get("model") or params.get("model_name") or metadata.get("ls_model_name"),
                "start_ms": _now_ms(self.t0),
                "end_ms": None,
                "prompt_tokens": None,
                "completion_tokens": None,
                "reasoning_tokens": None,
                "error": None,
            }

    def on_llm_end(self, response, *, run_id, **kwargs):
        usage = _usage(response)
        with self.lock:
            call = self.calls.get(run_id)
            if call is not None:
                call["end_ms"] = _now_ms(self.t0)
                call.update(usage)

    def on_llm_error(self, error, *, run_id, **kwargs):
        with self.lock:
            call = self.calls.get(run_id)
            if call is not None:
                call["end_ms"] = _now_ms(self.t0)
                call["error"] = f"{type(error).__name__}: {error}"[:500]

    # ----------------------------------------------------------------- result

    def result(self) -> dict:
        with self.lock:
            spans = sorted(self.spans.values(), key=lambda s: s["start_ms"])
            calls = sorted(self.calls.values(), key=lambda c: c["start_ms"])
        for item in (*spans, *calls):
            if item["end_ms"] is not None:
                item["ms"] = round(item["end_ms"] - item["start_ms"], 1)
        return {"spans": spans, "llm_calls": calls}


def _usage(response) -> dict:
    """Token usage of a chat completion, including DeepSeek's reasoning tokens."""
    usage: dict[str, Any] = {}
    try:
        message = response.generations[0][0].message
        meta = getattr(message, "usage_metadata", None) or {}
        usage["prompt_tokens"] = meta.get("input_tokens")
        usage["completion_tokens"] = meta.get("output_tokens")
        usage["reasoning_tokens"] = (meta.get("output_token_details") or {}).get("reasoning")
    except (AttributeError, IndexError, TypeError):
        pass
    token_usage = (response.llm_output or {}).get("token_usage") or {}
    if usage.get("prompt_tokens") is None:
        usage["prompt_tokens"] = token_usage.get("prompt_tokens")
        usage["completion_tokens"] = token_usage.get("completion_tokens")
    if usage.get("reasoning_tokens") is None:
        usage["reasoning_tokens"] = (token_usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
    return usage


def _summarize_output(node: str, outputs: Any) -> Any:
    """Keep the decision-relevant part of a node's state update."""
    if not isinstance(outputs, dict):
        return None
    if node == "generate_query":
        queries = outputs.get("search_query")
        queries = getattr(queries, "value", queries)  # Overwrite(...) wrapper
        return {"queries": list(queries or [])}
    if node == "reflection":
        return {
            key: outputs.get(key)
            for key in ("is_sufficient", "knowledge_gap", "follow_up_queries", "research_loop_count")
        }
    if node == "kb_retrieve":
        log = (outputs.get("retrieval_log") or [{}])[0]
        return {"query": log.get("query"), "passed": log.get("passed")}
    return None
