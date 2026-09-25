"""End-to-end smoke test against a RUNNING mock backend (`make mocks` first).

Unlike pytest (which runs the app in-process), this goes over real HTTP, the
same way the MCP server will. Exits non-zero on the first unexpected result.

    uv run python scripts/smoke_mocks.py      (or: make smoke)
"""

import sys
import uuid

import httpx

from telco_mcp_lab.mock_apis.settings import MockApiSettings


def check(label: str, resp: httpx.Response, expected: int) -> dict:
    ok = resp.status_code == expected
    print(f"{'PASS' if ok else 'FAIL'}  {label:<55} -> {resp.status_code}")
    if not ok:
        print(resp.text)
        sys.exit(1)
    return resp.json() if resp.content else {}


def main() -> None:
    s = MockApiSettings()  # type: ignore[call-arg]  # reads .env
    base = f"http://{s.host}:{s.port}"
    try:
        httpx.get(f"{base}/health", timeout=2).raise_for_status()
    except httpx.HTTPError:
        sys.exit(f"Mock backend not reachable at {base}. Start it with: make mocks")

    auth = {"X-Api-Key": s.api_key.get_secret_value()}
    with httpx.Client(base_url=base, headers=auth, timeout=10) as c:
        check("no API key is rejected", httpx.get(f"{base}/accounts/ACC-1001"), 401)
        acc = check("get account ACC-1001", c.get("/accounts/ACC-1001"), 200)
        print(f"      notes (raw, UNSAFE for an LLM): {acc['notes'][:70]}...")
        check(
            "list ACTIVE subscriptions",
            c.get("/accounts/ACC-1001/subscriptions?status=ACTIVE"),
            200,
        )
        check("service details", c.get("/services/SVC-1001-01"), 200)
        check("order status ORD-000123", c.get("/orders/ORD-000123"), 200)
        draft = check(
            "create draft (ACC-2001 add EU roaming)",
            c.post(
                "/orders/drafts",
                json={
                    "account_id": "ACC-2001",
                    "subscription_id": "SUB-2001-04",
                    "action": "ADD_ADDON",
                    "target_code": "ADDON-ROAM-EU",
                },
            ),
            201,
        )
        print(f"      price: {draft['price_summary']}, expires {draft['expires_at']}")
        key = f"smoke-{uuid.uuid4().hex}"
        body = {"draft_id": draft["draft_id"]}
        check("submit without Idempotency-Key", c.post("/order-submissions", json=body), 400)
        first = check(
            "submit with key",
            c.post("/order-submissions", json=body, headers={"Idempotency-Key": key}),
            201,
        )
        again = c.post("/order-submissions", json=body, headers={"Idempotency-Key": key})
        replay = check("replay same key -> same order", again, 200)
        assert replay == first and again.headers.get("Idempotent-Replayed") == "true"
        check(
            "same draft, new key -> 409",
            c.post(
                "/order-submissions",
                json=body,
                headers={"Idempotency-Key": f"smoke-{uuid.uuid4().hex}"},
            ),
            409,
        )
    print(f"\nAll smoke checks passed. Created order {first['order_id']}.")


if __name__ == "__main__":
    main()
