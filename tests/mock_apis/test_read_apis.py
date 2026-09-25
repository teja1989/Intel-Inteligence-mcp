"""Account, Subscription, Service and Order read APIs."""

import pytest

from telco_mcp_lab.mock_apis.data import INJECTED_NOTE


@pytest.mark.security
class TestServiceAuth:
    @pytest.mark.parametrize(
        "path",
        [
            "/accounts/ACC-1001",
            "/accounts/ACC-1001/subscriptions",
            "/services/SVC-1001-01",
            "/orders/ORD-000123",
            "/accounts/ACC-1001/orders",
            "/_admin/chaos",
        ],
    )
    def test_missing_key_is_rejected(self, anon_client, path):
        r = anon_client.get(path)
        assert r.status_code == 401
        assert r.headers["content-type"] == "application/problem+json"
        assert r.json()["code"] == "UNAUTHENTICATED"

    def test_wrong_key_is_rejected(self, anon_client):
        r = anon_client.get("/accounts/ACC-1001", headers={"X-Api-Key": "x" * 27})
        assert r.status_code == 401

    def test_health_is_public(self, anon_client):
        assert anon_client.get("/health").json() == {"status": "ok"}


class TestAccounts:
    def test_get_account_returns_raw_record_including_notes(self, client):
        body = client.get("/accounts/ACC-1001").json()
        assert body["holder_name"] == "Alex Example"
        # The backend returns the injection payload verbatim. That's the point:
        # neutralising it is the MCP server's job (Phase 3).
        assert body["notes"] == INJECTED_NOTE

    def test_unknown_account_is_problem_404(self, client):
        r = client.get("/accounts/ACC-9999")
        assert r.status_code == 404
        assert r.json()["code"] == "ACCOUNT_NOT_FOUND"

    @pytest.mark.security
    @pytest.mark.parametrize("bad", ["ACC-1", "acc-1001", "ACC-1001;DROP", "..%2F..%2Fetc"])
    def test_malformed_id_rejected_before_lookup(self, client, bad):
        r = client.get(f"/accounts/{bad}")
        assert r.status_code in (404, 422)
        assert r.headers["content-type"] == "application/problem+json"


class TestSubscriptions:
    def test_status_filter(self, client):
        items = client.get("/accounts/ACC-1001/subscriptions?status=ACTIVE").json()["items"]
        assert [s["subscription_id"] for s in items] == ["SUB-1001-01"]

    def test_invalid_status_is_validation_problem(self, client):
        r = client.get("/accounts/ACC-1001/subscriptions?status=ENABLED")
        assert r.status_code == 422
        assert r.json()["code"] == "VALIDATION_FAILED"

    def test_pagination_walks_all_pages_without_duplicates(self, client):
        seen, cursor = [], None
        while True:
            url = "/accounts/ACC-2001/subscriptions?limit=2" + (
                f"&cursor={cursor}" if cursor else ""
            )
            page = client.get(url).json()
            seen += [s["subscription_id"] for s in page["items"]]
            cursor = page["next_cursor"]
            if cursor is None:
                break
        assert seen == [f"SUB-2001-0{i}" for i in range(1, 6)]

    @pytest.mark.parametrize("cursor", ["not-base64!", "eyJ4IjoxfQ", "eyJvIjotMX0"])
    def test_tampered_cursor_is_rejected(self, client, cursor):
        r = client.get(f"/accounts/ACC-2001/subscriptions?cursor={cursor}")
        assert r.status_code == 400
        assert r.json()["code"] == "INVALID_CURSOR"

    def test_limit_is_bounded(self, client):
        assert client.get("/accounts/ACC-2001/subscriptions?limit=500").status_code == 422


class TestServicesAndOrders:
    def test_service_details(self, client):
        body = client.get("/services/SVC-1001-01").json()
        assert body["roaming_enabled"] is True
        assert body["addons"] == ["ADDON-ROAM-EU"]

    def test_get_seeded_order(self, client):
        assert client.get("/orders/ORD-000123").json()["status"] == "COMPLETED"

    def test_list_orders_newest_first_and_scoped_to_account(self, client):
        items = client.get("/accounts/ACC-1001/orders").json()["items"]
        assert [o["order_id"] for o in items] == ["ORD-000124", "ORD-000123"]
        assert all(o["account_id"] == "ACC-1001" for o in items)
