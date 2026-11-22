"""Protocol-level access-control checks against the running agent server and gateway.

    python -m eval.harness security

These are the permission boundaries that are not questions to the agent:
who may call the agent server, whose threads they can read, which models a
gateway token may use, and whose cost records a token can see. Each check is
deterministic and prints PASS, FAIL or SKIP.
"""

import json
import secrets
import urllib.error
import urllib.request

from eval.harness import runner  # noqa: F401  (loads the agent's .env)
from eval.harness.paths import BACKEND_DIR, EVAL_DIR, read_env

AGENT_URL = "http://localhost:2024"
GATEWAY_URL = "http://localhost:3000"
QUESTION = "生产网关的管理员令牌多久轮换一次？轮换确认口令是什么？"
CANARY = "OPS-CANARY-7F3K9Q"


def http(method: str, url: str, body=None, headers: dict | None = None, timeout: int = 300) -> tuple[int, object]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data, {"Content-Type": "application/json", **(headers or {})}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw.decode(errors="replace")[:200]


def bearer(key: str | None) -> dict:
    return {"Authorization": f"Bearer {key}"} if key else {}


def anonymous_session() -> str:
    """A fresh server-issued anonymous session token."""
    status, body = http("POST", f"{AGENT_URL}/anonymous-session", {})
    assert status == 200, (status, body)
    return body["token"]


def ask_agent(key: str | None, configurable: dict | None = None) -> tuple[str, dict]:
    key = key or anonymous_session()
    status, thread = http("POST", f"{AGENT_URL}/threads", {}, bearer(key))
    assert status == 200, (status, thread)
    body = {
        "assistant_id": "agent",
        "input": {"messages": [{"type": "human", "content": QUESTION}], "initial_search_query_count": 1, "max_research_loops": 1},
    }
    if configurable:
        body["config"] = {"configurable": configurable}
    status, state = http("POST", f"{AGENT_URL}/threads/{thread['thread_id']}/runs/wait", body, bearer(key))
    assert status == 200, (status, state)
    return thread["thread_id"], state


def groups_used(state: dict) -> list[str]:
    return sorted({g for entry in state.get("retrieval_log", []) for g in entry["groups"]})


def main() -> None:
    env = {**read_env(), **read_env(BACKEND_DIR / ".env")}
    ops_key = env.get("EVAL_AGENT_KEY_OPS")
    gateway_key = env.get("GATEWAY_API_KEY")
    restricted_key = env.get("EVAL_RESTRICTED_API_KEY")
    results: list[tuple[str, str, str]] = []

    def check(name: str, passed: bool | None, detail: str) -> None:
        verdict = "SKIP" if passed is None else ("PASS" if passed else "FAIL")
        results.append((name, verdict, detail))
        print(f"{verdict:4}  {name}: {detail}", flush=True)

    # Agent server: authentication and identity.
    status, _ = http("POST", f"{AGENT_URL}/threads", {})
    check("未携带凭证被拒绝", status == 401, f"HTTP {status}")
    status, _ = http("POST", f"{AGENT_URL}/threads", {}, bearer("agk-not-a-real-key"))
    check("未知 Agent 密钥被拒绝", status == 401, f"HTTP {status}")
    status, _ = http("POST", f"{AGENT_URL}/threads", {}, bearer("anon.chosen-by-client.00000000000000000000000000000000"))
    check("客户端自造的匿名会话被拒绝", status == 401, f"HTTP {status}")

    # Anonymous visitors are isolated from each other, not one shared identity.
    visitor_a, visitor_b = anonymous_session(), anonymous_session()
    status, thread = http("POST", f"{AGENT_URL}/threads", {}, bearer(visitor_a))
    a_thread = thread["thread_id"] if status == 200 else ""
    status, _ = http("GET", f"{AGENT_URL}/threads/{a_thread}", headers=bearer(visitor_b))
    check("匿名访客读不到其他匿名访客的线程", status == 404, f"HTTP {status}")
    status, found = http("POST", f"{AGENT_URL}/threads/search", {"limit": 100}, bearer(visitor_b))
    ids = {t["thread_id"] for t in found} if isinstance(found, list) else set()
    check("匿名访客搜索不到其他匿名访客的线程", status == 200 and a_thread not in ids, f"HTTP {status}，结果含对方线程={a_thread in ids}")
    status, _ = http("POST", f"{AGENT_URL}/threads/{a_thread}/runs/wait",
                     {"assistant_id": "agent", "input": {"messages": [{"type": "human", "content": "hi"}]}}, bearer(visitor_b))
    check("匿名访客不能在其他匿名访客的线程上运行", status == 404, f"HTTP {status}")
    status, _ = http("GET", f"{AGENT_URL}/threads/{a_thread}", headers=bearer(visitor_a))
    check("匿名访客能读自己的线程（对照）", status == 200, f"HTTP {status}")

    _, state = ask_agent(None, {"langgraph_auth_user": {"identity": "attacker", "groups": ["ops"]}})
    answer = state["messages"][-1]["content"]
    check("请求体伪造身份无效", groups_used(state) == ["public"] and CANARY not in answer,
          f"检索分组 {groups_used(state)}，回答含口令={CANARY in answer}")

    _, state = ask_agent(None, {"user_groups": ["ops"], "groups": ["ops"]})
    check("普通配置项伪造分组无效", groups_used(state) == ["public"], f"检索分组 {groups_used(state)}")

    if ops_key:
        ops_thread, state = ask_agent(ops_key)
        answer = state["messages"][-1]["content"]
        check("ops 身份可读内部文档（对照）", groups_used(state) == ["ops", "public"] and CANARY in answer,
              f"检索分组 {groups_used(state)}，回答含口令={CANARY in answer}")
        status, _ = http("GET", f"{AGENT_URL}/threads/{ops_thread}", headers=bearer(visitor_a))
        check("匿名访客读不到 ops 的线程", status == 404, f"HTTP {status}")
        status, found = http("POST", f"{AGENT_URL}/threads/search", {"limit": 100}, bearer(visitor_a))
        ids = {t["thread_id"] for t in found} if isinstance(found, list) else set()
        check("匿名访客搜索不到 ops 的线程", status == 200 and ops_thread not in ids, f"HTTP {status}，结果含 ops 线程={ops_thread in ids}")
        status, _ = http("POST", f"{AGENT_URL}/threads/{ops_thread}/runs/wait",
                         {"assistant_id": "agent", "input": {"messages": [{"type": "human", "content": "hi"}]}}, bearer(visitor_a))
        check("匿名访客不能在 ops 的线程上运行", status == 404, f"HTTP {status}")
    else:
        check("ops 身份相关检查", None, "缺少 EVAL_AGENT_KEY_OPS（运行 python -m eval.harness.agent_users）")

    # Gateway: model allowlist and per-token cost isolation.
    if restricted_key:
        # A run id of its own, so calls from earlier checks are not counted.
        run_id = f"security.isolation.{secrets.token_hex(4)}"
        status, body = http("POST", f"{GATEWAY_URL}/v1/chat/completions",
                            {"model": "deepseek-flash", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5},
                            bearer(restricted_key), timeout=60)
        check("受限令牌调用白名单外模型被拒绝", status in (401, 403, 400) and status != 200,
              f"HTTP {status} {str(body)[:80]}")
        status, body = http("POST", f"{GATEWAY_URL}/v1/chat/completions",
                            {"model": "qwen3:4b-instruct-2507-q4_K_M", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5},
                            {**bearer(restricted_key), "X-Agent-Run-Id": run_id, "X-Agent-Node": "probe"}, timeout=120)
        check("受限令牌调用白名单内模型成功（对照）", status == 200, f"HTTP {status}")
        status, body = http("GET", f"{GATEWAY_URL}/api/agent_runs/{run_id}/cost", headers=bearer(gateway_key))
        calls = body.get("data", {}).get("calls") if isinstance(body, dict) else None
        check("令牌 A 查不到令牌 B 的运行成本", status == 200 and calls == 0, f"HTTP {status}，可见调用数={calls}")
        status, body = http("GET", f"{GATEWAY_URL}/api/agent_runs/{run_id}/cost", headers=bearer(restricted_key))
        calls = body.get("data", {}).get("calls") if isinstance(body, dict) else None
        check("令牌 B 能查到自己的运行成本（对照）", status == 200 and calls == 1, f"HTTP {status}，可见调用数={calls}")
    else:
        check("网关模型白名单与成本隔离", None, "缺少 EVAL_RESTRICTED_API_KEY（在网关后台建 eval-restricted 令牌）")

    status, _ = http("GET", f"{GATEWAY_URL}/api/agent_runs/any/cost")
    check("成本接口未带令牌被拒绝", status == 401, f"HTTP {status}")

    lines = ["# 访问控制检查", "", "| 检查 | 结果 | 详情 |", "|---|---|---|"]
    lines += [f"| {name} | {verdict} | {detail} |" for name, verdict, detail in results]
    path = EVAL_DIR / "results" / "security.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"-> {path}")


if __name__ == "__main__":
    main()
