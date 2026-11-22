"""Fault-injection proxy in front of the gateway, keyed by the X-Agent-Node header.

The agent tags every model and embedding call with the graph node that made
it, so faults can be injected into one stage at a time without touching the
agent. Rules are replaced with ``POST /__chaos``:

    {"rules": {"finalize_answer": {"fault": "status", "status": 500}}}
    {"rules": {"reflection": {"fault": "delay", "seconds": 20}}}
    {"rules": {"generate_query": {"fault": "drop"}}}            # connection closed, no response
    {"rules": {"reflection": {"fault": "garbage"}}}             # 200 with no usable output
    {"rules": {"*": {"fault": "status", "status": 500, "p": 0.3}}}   # flaky, every node
    {"rules": {"finalize_answer": {"fault": "status", "status": 402, "model": "deepseek-flash"}}}   # one model only

``GET /__chaos`` returns the rules and how many faults were injected per node.
"""

import http.client
import json
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = ("localhost", 3000)
HOP_HEADERS = {"connection", "keep-alive", "transfer-encoding", "content-length", "host"}


class ChaosState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.rules: dict[str, dict] = {}
        self.injected: dict[str, int] = {}
        self.rng = random.Random(0)

    def pick(self, node: str, model: str) -> dict | None:
        with self.lock:
            rule = self.rules.get(node) or self.rules.get("*")
            if rule is None or rule.get("model", model) != model or self.rng.random() >= rule.get("p", 1.0):
                return None
            self.injected[node] = self.injected.get(node, 0) + 1
            return rule


STATE = ChaosState()


GARBAGE = "I am not able to help with that."


def _garbage_completion(model: str) -> bytes:
    """A well-formed chat completion whose content parses as neither JSON nor a tool call."""
    return json.dumps({
        "id": "chaos", "object": "chat.completion", "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": GARBAGE}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 8, "total_tokens": 9},
    }).encode()


def _garbage_stream(model: str) -> bytes:
    """The same unusable answer as a streamed (server-sent events) chat completion."""
    def chunk(delta: dict, finish: str | None) -> str:
        body = {"id": "chaos", "object": "chat.completion.chunk", "created": int(time.time()), "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        return f"data: {json.dumps(body)}\n\n"
    return (chunk({"role": "assistant", "content": GARBAGE}, None) + chunk({}, "stop") + "data: [DONE]\n\n").encode()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # keep the eval output clean
        pass

    def _reply(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        if self.path == "/__chaos":
            if self.command == "POST":
                with STATE.lock:
                    STATE.rules = json.loads(body or b"{}").get("rules", {})
                    STATE.injected = {}
            with STATE.lock:
                payload = {"rules": STATE.rules, "injected": STATE.injected}
            return self._reply(200, json.dumps(payload).encode())

        try:
            request = json.loads(body or b"{}") or {}
        except ValueError:
            request = {}
        model = request.get("model", "") if isinstance(request, dict) else ""
        rule = STATE.pick(self.headers.get("X-Agent-Node", ""), model)
        if rule is not None:
            fault = rule["fault"]
            if fault == "drop":
                self.close_connection = True
                return
            if fault == "status":
                error = {"error": {"message": f"chaos: injected {rule['status']}", "type": "chaos"}}
                return self._reply(rule["status"], json.dumps(error).encode())
            if fault == "garbage":
                if isinstance(request, dict) and request.get("stream"):
                    return self._reply(200, _garbage_stream(model), "text/event-stream")
                return self._reply(200, _garbage_completion(model))
            if fault == "delay":
                time.sleep(rule["seconds"])

        upstream = http.client.HTTPConnection(*UPSTREAM, timeout=600)
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_HEADERS}
        upstream.request(self.command, self.path, body=body or None, headers=headers)
        response = upstream.getresponse()
        data = response.read()
        self.send_response(response.status)
        for key, value in response.getheaders():
            if key.lower() not in HOP_HEADERS:
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        upstream.close()

    do_GET = do_POST = _handle


def start(port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main() -> None:
    """Run the proxy, or switch a running proxy to one of the fault scenarios.

        python -m eval.harness.chaos_proxy serve [port]
        python -m eval.harness.chaos_proxy set answer-402      # a scenario id from eval.harness.faults
        python -m eval.harness.chaos_proxy set none            # pass everything through
    """
    import sys

    from eval.harness import faults

    command = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if command == "serve":
        port = int(sys.argv[2]) if len(sys.argv) > 2 else faults.PROXY_PORT
        start(port)
        print(f"chaos proxy on :{port} -> {UPSTREAM[0]}:{UPSTREAM[1]}", flush=True)
        threading.Event().wait()
    elif command == "set":
        scenarios = {s["id"]: s for s in faults.SCENARIOS if not s.get("env")}
        name = sys.argv[2] if len(sys.argv) > 2 else "none"
        if name not in scenarios:
            raise SystemExit(f"unknown scenario {name!r}; choose from: {', '.join(scenarios)}")
        faults.set_rules(scenarios[name]["rules"])
        # Scenario ids only: labels are Chinese and Windows consoles may not encode them.
        print(f"chaos scenario set: {name}")
    else:
        raise SystemExit(main.__doc__)


if __name__ == "__main__":
    main()
