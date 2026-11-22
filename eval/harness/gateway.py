"""Gateway-side view of a run: the per-node cost the gateway attributed to it."""

import json
import time
import urllib.error
import urllib.request

GATEWAY_URL = "http://localhost:3000"

# USD per million tokens (input, output, cache hit), matching the gateway's
# model pricing: DeepSeek's peak-hour prices, local models free.
PRICES = {
    "deepseek-flash": (0.3, 1.2, 0.006),
    "deepseek-v4-pro": (1.32, 3.96, 0.044),
    "qwen3:4b-instruct-2507-q4_K_M": (0.0, 0.0, 0.0),
    "bge-m3": (0.0, 0.0, 0.0),
}
# What local calls would have cost on the cloud answer model.
CLOUD_REFERENCE = "deepseek-flash"


def call_cost(model: str | None, prompt_tokens: int | None, completion_tokens: int | None, reference: bool = False) -> float:
    price_in, price_out, _ = PRICES[CLOUD_REFERENCE] if reference else PRICES.get(model or "", (0.0, 0.0, 0.0))
    return ((prompt_tokens or 0) * price_in + (completion_tokens or 0) * price_out) / 1_000_000


def run_cost(api_key: str, run_id: str, expect_calls: int, attempts: int = 4) -> dict | None:
    """Fetch GET /api/agent_runs/:id/cost, waiting briefly for logs written after the response."""
    request = urllib.request.Request(
        f"{GATEWAY_URL}/api/agent_runs/{run_id}/cost",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    data = None
    for attempt in range(attempts):
        time.sleep(1.0 + 2 * attempt)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = json.loads(response.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:  # per-user search rate limit
                time.sleep(15)
                continue
            raise
        data = body.get("data")
        if data and data["calls"] + data["errors"] >= expect_calls:
            break
    return data
