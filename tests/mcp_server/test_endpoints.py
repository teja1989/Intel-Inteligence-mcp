"""The gateway endpoint catalogue (telco_mcp/endpoints.py): templates, env overrides, safety.

What matters in production: an environment can change every path and query parameter
name with GATEWAY_ENDPOINT_*, but can never point a call (and the server's gateway
token) at another host, never drop a parameter silently, and IDs can never escape
their path segment.
"""

import httpx
import pytest
import respx
from pydantic import ValidationError

from telco_mcp.endpoints import OPERATIONS, GatewayEndpoints, Template
from tests.conftest import GATEWAY_URL, make_telco

ACCOUNT = frozenset({"account_id"})


def endpoints(**overrides: str) -> GatewayEndpoints:
    return GatewayEndpoints(_env_file=None, **overrides)


class TestCatalogue:
    def test_every_operation_has_a_setting_and_valid_default(self):
        ep = endpoints()
        assert set(OPERATIONS) == set(GatewayEndpoints.model_fields)
        assert ep.defaulted() == list(OPERATIONS) and ep.overrides() == {}

    def test_default_urls_match_the_lab_gateway_convention(self):
        call = endpoints().resolve("get_subscription", subscription_id="SUB-1001-01")
        assert (call.method, call.path, call.params) == (
            "GET", "/bosubscription/API/subscription/SUB-1001-01", [],
        )  # fmt: skip
        assert call.route == "/bosubscription/API/subscription/{subscription_id}"

    def test_none_query_values_are_left_out(self):
        call = endpoints().resolve(
            "list_subscriptions", account_id="ACC-1001", status=None, limit=5, cursor=None
        )
        assert call.params == [("account_id", "ACC-1001"), ("limit", "5")]

    def test_resolve_requires_exactly_the_operation_parameters(self):
        with pytest.raises(TypeError):
            endpoints().resolve("get_account")
        with pytest.raises(TypeError):
            endpoints().resolve("get_account", account_id="ACC-1001", extra="x")


class TestEnvironmentOverrides:
    def test_env_var_per_operation(self, monkeypatch):
        monkeypatch.setenv("GATEWAY_ENDPOINT_GET_ACCOUNT", "/customer/v2/accounts/{account_id}")
        ep = GatewayEndpoints(_env_file=None)
        assert ep.overrides() == {"get_account": "/customer/v2/accounts/{account_id}"}
        assert "get_account" not in ep.defaulted()
        assert ep.resolve("get_account", account_id="ACC-1001").path == (
            "/customer/v2/accounts/ACC-1001"
        )

    def test_nested_paths_renamed_query_params_and_literals(self):
        ep = endpoints(list_subscriptions=(
            "/customer/v2/accounts/{account_id}/lines"
            "?state={status}&pageSize={limit}&pageToken={cursor}&expand=plan"
        ))  # fmt: skip
        call = ep.resolve(
            "list_subscriptions", account_id="ACC-1001", status="ACTIVE", limit=5, cursor=None
        )
        assert call.path == "/customer/v2/accounts/ACC-1001/lines"
        assert call.params == [("state", "ACTIVE"), ("pageSize", "5"), ("expand", "plan")]

    @respx.mock
    async def test_the_client_calls_the_configured_endpoint(self):
        """End to end through TelcoApiClient: the override is what goes on the wire."""
        route = respx.get(f"{GATEWAY_URL}/crm/v3/accounts/ACC-1001/orders").mock(
            return_value=httpx.Response(200, json={"items": [], "next_cursor": None})
        )
        client = make_telco()
        client._endpoints = endpoints(
            list_orders="/crm/v3/accounts/{account_id}/orders?size={limit}&after={cursor}"
        )
        await client.list_orders("ACC-1001", limit=3)
        await client.aclose()
        assert dict(route.calls.last.request.url.params) == {"size": "3"}


@pytest.mark.security
class TestTemplateSafety:
    @pytest.mark.parametrize(
        ("template", "error"),
        [
            ("https://evil.example/a/{account_id}", "must be a path"),
            ("//evil.example/a/{account_id}", "must be a path"),
            ("/a/{account_id}#x", "must be a path"),
            ("a/{account_id}", "must start with /"),
            ("/a/../{account_id}", "'..'"),
            ("/a//{account_id}", "empty"),
            ("/a/%2e%2e/{account_id}", "must start with /"),
            ("/a b/{account_id}", "must start with /"),
            ("/a/{account_id", "malformed placeholder"),
            ("/a/{acount_id}", "missing placeholders ['account_id']"),
            ("/a", "missing placeholders"),
            ("/a/{account_id}/{account_id}", "more than once"),
            ("/a/{account_id}?id={account_id}", "more than once"),
            ("/a/{account_id}?x={status}", "unknown placeholders ['status']"),
            ("/a/{account_id}?x=a b", "plain literal"),
            ("/a/{account_id}?novalue", "name=value"),
        ],
    )  # fmt: skip
    def test_bad_templates_are_refused(self, template, error):
        with pytest.raises(ValueError, match="GATEWAY_ENDPOINT_GET_ACCOUNT") as exc:
            Template.parse("get_account", template, ACCOUNT)
        assert error in str(exc.value)

    def test_every_bad_setting_is_reported_at_once(self):
        with pytest.raises(ValidationError) as exc:
            endpoints(get_account="https://x/{account_id}", get_order="/o/{oder_id}")
        text = str(exc.value)
        assert "GATEWAY_ENDPOINT_GET_ACCOUNT" in text and "GATEWAY_ENDPOINT_GET_ORDER" in text

    @pytest.mark.parametrize("value", ["../../boorder/API/order", "ACC-1001?x=1", "a/b#c"])
    def test_ids_cannot_escape_their_segment(self, value):
        path = endpoints().resolve("get_account", account_id=value).path
        assert path.startswith("/boaccount/API/account/")
        assert "/" not in path.removeprefix("/boaccount/API/account/")
        assert "?" not in path and "#" not in path

    @pytest.mark.parametrize("value", ["", ".", ".."])
    def test_empty_and_dot_path_values_refused(self, value):
        with pytest.raises(ValueError):
            endpoints().resolve("get_account", account_id=value)

    def test_host_always_comes_from_the_base_url(self):
        """No template can change scheme/host: the resolved path is always host-relative."""
        for name in OPERATIONS:
            route = endpoints().model_dump()[name]
            assert route.startswith("/") and not route.startswith("//")
