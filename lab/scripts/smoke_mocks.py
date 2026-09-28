"""End-to-end smoke test against a RUNNING mock gateway (`make mocks` first).

Unlike pytest (which runs the app in-process), this goes over real HTTP using
the MCP server's own endpoint catalogue (telco_mcp/endpoints.py, defaults) and
GatewayBearerAuth, the same code path the tools use. The write endpoints (draft,
submission) aren't in the catalogue yet (Phase 4), so they use the mock's prefixes.
Exits non-zero on the first unexpected result.

    uv run python lab/scripts/smoke_mocks.py      (or: make smoke)
"""

import sys
import uuid

import httpx

from telco_mcp.clients.gateway import (
    GatewayBearerAuth,
    GatewayClientSettings,
    StaticTokenProvider,
)
from telco_mcp.endpoints import GatewayEndpoints
from telco_mcp_lab.mock_apis.app import MOCK_PREFIXES


def check(label: str, resp: httpx.Response, expected: int) -> dict:
    ok = resp.status_code == expected
    print(
        f"{'PASS' if ok else 'FAIL'}  {label:<48} {resp.request.method:<4} "
        f"{resp.request.url.path:<45} -> {resp.status_code}"
    )
    if not ok:
        print(resp.text)
        sys.exit(1)
    return resp.json() if resp.content else {}


def main() -> None:
    s = GatewayClientSettings()  # type: ignore[call-arg]  # reads .env (+ localhost guard)
    endpoints = GatewayEndpoints(_env_file=None)  # the mock serves the default paths

    def ep(operation: str, **values: object) -> httpx.URL:
        call = endpoints.resolve(operation, **values)
        return httpx.URL(s.base_url + call.path, params=call.params)

    try:
        httpx.get(f"{s.base_url}/health", timeout=2).raise_for_status()
    except httpx.HTTPError:
        sys.exit(f"Mock gateway not reachable at {s.base_url}. Start it with: make mocks")

    acc_url = ep("get_account", account_id="ACC-1001")
    auth = GatewayBearerAuth(StaticTokenProvider(s.token))
    with httpx.Client(auth=auth, timeout=10) as c:
        check("no token is rejected", httpx.get(acc_url), 401)
        acc = check("get account ACC-1001", c.get(acc_url), 200)
        print(f"      notes (raw, UNSAFE for an LLM): {acc['notes'][:70]}...")
        subs = ep(
            "list_subscriptions", account_id="ACC-1001", status="ACTIVE", limit=5, cursor=None
        )
        check("list ACTIVE subscriptions", c.get(subs), 200)
        check("service details", c.get(ep("get_service", service_id="SVC-1001-01")), 200)
        check("order status ORD-000123", c.get(ep("get_order", order_id="ORD-000123")), 200)
        draft = check(
            "create draft (ACC-2001 add EU roaming)",
            c.post(
                f"{s.base_url}{MOCK_PREFIXES['order']}/order/draft",
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
        submit_url = f"{s.base_url}{MOCK_PREFIXES['order_submission']}/submission"
        body = {"draft_id": draft["draft_id"]}
        key = f"smoke-{uuid.uuid4().hex}"
        check("submit without Idempotency-Key", c.post(submit_url, json=body), 400)
        first = check(
            "submit with key", c.post(submit_url, json=body, headers={"Idempotency-Key": key}), 201
        )
        again = c.post(submit_url, json=body, headers={"Idempotency-Key": key})
        replay = check("replay same key -> same order", again, 200)
        assert replay == first and again.headers.get("Idempotent-Replayed") == "true"
        other_key = {"Idempotency-Key": f"smoke-{uuid.uuid4().hex}"}
        check("same draft, new key -> 409", c.post(submit_url, json=body, headers=other_key), 409)
    print(f"\nAll smoke checks passed. Created order {first['order_id']}.")


if __name__ == "__main__":
    main()
