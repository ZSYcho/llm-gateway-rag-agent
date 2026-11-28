"""Run dataset cases against the agent graph in-process and record what the metrics need.

The graph is the one the server runs (``agent.graph.builder``), compiled with
an in-memory checkpointer so multi-turn cases share a thread. Each turn gets
its own run id, sent to the gateway as ``X-Agent-Run-Id``, so the gateway's
per-node cost can be joined back to the run.
"""

import os
import sys
import time
import uuid
from typing import Any

from dotenv import load_dotenv

from eval.harness.paths import BACKEND_DIR

sys.path.insert(0, str(BACKEND_DIR / "src"))
load_dotenv(BACKEND_DIR / ".env")

from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402

import agent.kb  # noqa: E402
import agent.llm  # noqa: E402
from agent.graph import builder  # noqa: E402
from eval.harness import gateway  # noqa: E402
from eval.harness.errors import classify  # noqa: E402
from eval.harness.trace import RunTracer  # noqa: E402

LOCAL_MODEL = "qwen3:4b-instruct-2507-q4_K_M"
CLOUD_MODEL = "deepseek-flash"

# reasoning_model picks the model of every LLM node; "auto" keeps per-node routing.
PROFILES = {
    "routed": {"reasoning_model": "auto", "nodes": {"generate_query": LOCAL_MODEL, "reflection": LOCAL_MODEL, "finalize_answer": CLOUD_MODEL}},
    "cloud": {"reasoning_model": CLOUD_MODEL, "nodes": {"generate_query": CLOUD_MODEL, "reflection": CLOUD_MODEL, "finalize_answer": CLOUD_MODEL}},
    "local": {"reasoning_model": LOCAL_MODEL, "nodes": {"generate_query": LOCAL_MODEL, "reflection": LOCAL_MODEL, "finalize_answer": LOCAL_MODEL}},
}

# Stand-ins for the identity the server derives from an agent API key.
IDENTITIES = {
    "anonymous": None,
    "ops": {"identity": "eval-ops", "groups": ["ops"]},
    "redteam": {"identity": "eval-redteam", "groups": ["redteam"]},
}


class Runner:
    def __init__(self, tag: str, profile: str, overrides: dict[str, Any], gateway_url: str | None = None):
        self.tag = tag
        self.profile = profile
        self.overrides = overrides
        self.graph = builder.compile(checkpointer=InMemorySaver(), name="eval-agent")
        if gateway_url:
            # Fault injection: route model and embedding calls through a proxy.
            agent.llm.GATEWAY_BASE_URL = gateway_url
            agent.kb.GATEWAY_BASE_URL = gateway_url

    def run_case(self, case: dict, rep: int) -> dict:
        thread_id = str(uuid.uuid4())
        turns = []
        for index, question in enumerate(case["turns"]):
            turns.append(self._run_turn(case, rep, thread_id, index, question))
            if turns[-1]["status"] != "ok":
                break
        return {
            "tag": self.tag,
            "case_id": case["id"],
            "suite": case["suite"],
            "profile": self.profile,
            "overrides": self.overrides,
            "rep": rep,
            "identity": case.get("identity", "anonymous"),
            "turns": turns,
            # Metrics and judges score the last turn.
            **{key: turns[-1][key] for key in ("status", "run_id", "answer", "error", "duration_s")},
        }

    def _run_turn(self, case: dict, rep: int, thread_id: str, index: int, question: str) -> dict:
        # The suffix keeps a rerun of the same tag from sharing gateway records with an earlier attempt.
        run_id = f"eval.{self.tag}.{case['id']}.r{rep}.t{index}.{uuid.uuid4().hex[:6]}"[-128:]
        tracer = RunTracer()
        configurable = {
            "thread_id": thread_id,
            "run_id": run_id,
            **self.overrides,
            **(case.get("configurable") or {}),
        }
        identity = IDENTITIES[case.get("identity", "anonymous")]
        if identity is not None:
            configurable["langgraph_auth_user"] = identity
        config = {"configurable": configurable, "callbacks": [tracer], "recursion_limit": 50}
        inputs = {
            "messages": [{"type": "human", "content": question}],
            "reasoning_model": PROFILES[self.profile]["reasoning_model"],
            **(case.get("state") or {}),
        }
        started = time.perf_counter()
        state, error = {}, None
        try:
            state = self.graph.invoke(inputs, config)
        except Exception as exc:  # recorded and classified, never fatal to the batch
            error = classify(exc)
        duration = round(time.perf_counter() - started, 2)
        trace = tracer.result()
        if error is not None:
            failed = [span["node"] for span in trace["spans"] if span["error"]]
            error["stage"] = failed[0] if failed else "unknown"
            if not failed and error["type"] == "recursion_limit":
                error["stage"] = "graph"
        messages = state.get("messages") or []
        return {
            "index": index,
            "question": question,
            "run_id": run_id,
            "status": "ok" if error is None else "error",
            "error": error,
            "duration_s": duration,
            "answer": messages[-1].content if messages and error is None else None,
            "citations": state.get("citations") or [],
            "degraded": state.get("degraded") or [],
            "blocked_links": state.get("blocked_links") or [],
            "context_chunk_ids": state.get("context_chunk_ids") or [],
            "search_query": state.get("search_query") or [],
            "retrieval_log": state.get("retrieval_log") or [],
            "sources": [
                {"chunk_id": int(s["short_url"][5:]), "score": s["score"], "corpus": s["corpus"], "query": s["query"],
                 **({"relevance": s["relevance"]} if "relevance" in s else {})}
                for s in state.get("sources_gathered") or []
            ],
            "trace": trace,
            "gateway": self._gateway_cost(run_id, trace, state),
        }

    @staticmethod
    def _gateway_cost(run_id: str, trace: dict, state: dict) -> dict | None:
        expected = len(trace["llm_calls"]) + len(state.get("retrieval_log") or [])
        if expected == 0:
            return None
        try:
            return gateway.run_cost(os.environ["GATEWAY_API_KEY"], run_id, expected)
        except Exception as exc:  # the gateway view is a cross-check, not required
            return {"error": classify(exc)}
