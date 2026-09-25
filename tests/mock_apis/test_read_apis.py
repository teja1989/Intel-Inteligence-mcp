"""Account, Subscription, Service and Order read APIs."""

import pytest
from fastapi.testclient import TestClient

from telco_mcp_lab.gateway_routes import GatewayRoutes
from telco_mcp_lab.mock_apis.app import create_app
from telco_mcp_lab.mock_apis.data import INJECTED_NOTE
from tests.conftest import AUTH, TEST_TOKEN


@pytest.mark.security
class TestGatewayAuth:
    PROTECTED = [
        "/boaccount/API/account/ACC-1001",
        "/bosubscription/API/subscription?account_id=ACC-1001",
        "/boservice/API/service/SVC-1001-01",
        "/boorder/API/order/ORD-000123",
        "/boorder/API/order?account_id=ACC-1001",
        "/_admin/chaos",
    ]

    @pytest.mark.parametrize("path", PROTECTED)
    def test_missing_token_is_401_with_bearer_challenge(self, anon_client, path):
        r = anon_client.get(path)
        assert r.status_code == 401
        assert r.headers["content-type"] == "application/problem+json"
        assert r.headers["www-authenticate"] == 'Bearer realm="mock-gateway"'
        assert r.json()["code"] == "UNAUTHENTICATED"

    def test_wrong_token_is_invalid_token(self, anon_client):
        r = anon_client.get(
            "/boaccount/API/account/ACC-1001", headers={"Authorization": "Bearer " + "x" * 27}
        )
        assert r.status_code == 401
        assert 'error="invalid_token"' in r.headers["www-authenticate"]
        assert r.json()["code"] == "INVALID_TOKEN"

    @pytest.mark.parametrize(
        "header",
        [
            f"Basic {TEST_TOKEN}",  # wrong scheme
            TEST_TOKEN,  # no scheme
            "Bearer",  # no token
            "Bearer ",
        ],
    )
    def test_malformed_authorization_rejected(self, anon_client, header):
        r = anon_client.get("/boaccount/API/account/ACC-1001", headers={"Authorization": header})
        assert r.status_code == 401

    def test_scheme_is_case_insensitive(self, anon_client):
        r = anon_client.get(
            "/boaccount/API/account/ACC-1001", headers={"Authorization": f"bearer {TEST_TOKEN}"}
        )
        assert r.status_code == 200

    def test_token_never_echoed_in_error(self, anon_client):
        guess = "Bearer guessed-token-value-123456"
        r = anon_client.get("/boaccount/API/account/ACC-1001", headers={"Authorization": guess})
        assert "guessed-token-value" not in r.text

    def test_health_is_public(self, anon_client):
        assert anon_client.get("/health").json() == {"status": "ok"}


class TestGatewayRouting:
    def test_trailing_slash_is_accepted(self, client):
        # The user's gateway example: gateway/bosubscription/API/subscription/
        r = client.get("/bosubscription/API/subscription/?account_id=ACC-1001")
        assert r.status_code == 200
        assert client.get("/boaccount/API/account/ACC-1001/").status_code == 200

    def test_old_flat_paths_do_not_exist(self, client):
        r = client.get("/accounts/ACC-1001")
        assert r.status_code == 404
        assert r.json()["code"] == "NOT_FOUND"

    def test_microservice_names_come_from_config(self, settings, clock):
        routes = GatewayRoutes(_env_file=None, svc_account="crm-account", api_segment="v2")
        app = create_app(settings, routes, clock=clock)
        with TestClient(app, headers=AUTH) as c:
            assert c.get("/crm-account/v2/account/ACC-1001").status_code == 200
            assert c.get("/boaccount/API/account/ACC-1001").status_code == 404

    def test_unsafe_microservice_name_rejected_at_startup(self):
        with pytest.raises(ValueError):
            GatewayRoutes(_env_file=None, svc_account="../admin")


class TestAccounts:
    def test_get_account_returns_raw_record_including_notes(self, client):
        body = client.get("/boaccount/API/account/ACC-1001").json()
        assert body["holder_name"] == "Alex Example"
        # The backend returns the injection payload verbatim. That's the point:
        # neutralising it is the MCP server's job (Phase 3).
        assert body["notes"] == INJECTED_NOTE

    def test_unknown_account_is_problem_404(self, client):
        r = client.get("/boaccount/API/account/ACC-9999")
        assert r.status_code == 404
        assert r.json()["code"] == "ACCOUNT_NOT_FOUND"

    @pytest.mark.security
    @pytest.mark.parametrize("bad", ["ACC-1", "acc-1001", "ACC-1001;DROP", "..%2F..%2Fetc"])
    def test_malformed_id_rejected_before_lookup(self, client, bad):
        r = client.get(f"/boaccount/API/account/{bad}")
        assert r.status_code in (404, 422)
        assert r.headers["content-type"] == "application/problem+json"


class TestSubscriptions:
    def test_status_filter(self, client):
        items = client.get(
            "/bosubscription/API/subscription?account_id=ACC-1001&status=ACTIVE"
        ).json()["items"]
        assert [s["subscription_id"] for s in items] == ["SUB-1001-01"]

    def test_invalid_status_is_validation_problem(self, client):
        r = client.get("/bosubscription/API/subscription?account_id=ACC-1001&status=ENABLED")
        assert r.status_code == 422
        assert r.json()["code"] == "VALIDATION_FAILED"

    def test_pagination_walks_all_pages_without_duplicates(self, client):
        seen, cursor = [], None
        while True:
            url = "/bosubscription/API/subscription?account_id=ACC-2001&limit=2" + (
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
        r = client.get(f"/bosubscription/API/subscription?account_id=ACC-2001&cursor={cursor}")
        assert r.status_code == 400
        assert r.json()["code"] == "INVALID_CURSOR"

    def test_limit_is_bounded(self, client):
        assert (
            client.get("/bosubscription/API/subscription?account_id=ACC-2001&limit=500").status_code
            == 422
        )


class TestServicesAndOrders:
    def test_service_details(self, client):
        body = client.get("/boservice/API/service/SVC-1001-01").json()
        assert body["roaming_enabled"] is True
        assert body["addons"] == ["ADDON-ROAM-EU"]

    def test_get_seeded_order(self, client):
        assert client.get("/boorder/API/order/ORD-000123").json()["status"] == "COMPLETED"

    def test_list_orders_newest_first_and_scoped_to_account(self, client):
        items = client.get("/boorder/API/order?account_id=ACC-1001").json()["items"]
        assert [o["order_id"] for o in items] == ["ORD-000124", "ORD-000123"]
        assert all(o["account_id"] == "ACC-1001" for o in items)
