"""Contract: every DEFAULT endpoint of the server's catalogue exists on the mock gateway.

The mock's paths are the catalogue defaults, so local runs need no GATEWAY_ENDPOINT_*
settings. If either side changes, this fails.
"""

import pytest

from telco_mcp.endpoints import OPERATIONS, GatewayEndpoints

SAMPLE = {
    "account_id": "ACC-1001",
    "subscription_id": "SUB-1001-01",
    "service_id": "SVC-1001-01",
    "order_id": "ORD-000123",
    "status": "ACTIVE",
    "limit": 2,
    "cursor": None,
}


@pytest.mark.parametrize("operation", sorted(OPERATIONS))
def test_default_endpoint_is_served_by_the_mock(client, operation):
    op = OPERATIONS[operation]
    call = GatewayEndpoints(_env_file=None).resolve(operation, **{p: SAMPLE[p] for p in op.params})
    r = client.request(call.method, call.path, params=call.params)
    assert r.status_code == 200, (operation, r.status_code, r.text[:200])
