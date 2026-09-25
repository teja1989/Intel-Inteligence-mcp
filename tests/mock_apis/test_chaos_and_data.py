"""Chaos switch behaviour, and guards that the synthetic data stays synthetic."""

import re
import time

import pytest

from telco_mcp_lab.mock_apis import data


class TestChaos:
    @pytest.mark.slow
    def test_delay_is_applied(self, client):
        client.post("/_admin/chaos", json={"delay_ms": 300})
        start = time.perf_counter()
        assert client.get("/accounts/ACC-1001").status_code == 200
        assert time.perf_counter() - start >= 0.3

    def test_failure_injection_returns_problem(self, client):
        client.post("/_admin/chaos", json={"fail_rate": 1.0, "fail_status": 503})
        r = client.get("/accounts/ACC-1001")
        assert r.status_code == 503
        assert r.json()["code"] == "INJECTED_FAILURE"

    def test_health_and_admin_are_exempt(self, client):
        client.post("/_admin/chaos", json={"fail_rate": 1.0})
        assert client.get("/health").status_code == 200
        assert client.post("/_admin/chaos", json={}).json()["fail_rate"] == 0.0
        assert client.get("/accounts/ACC-1001").status_code == 200

    def test_admin_input_is_bounded(self, client):
        assert client.post("/_admin/chaos", json={"fail_rate": 2}).status_code == 422
        assert client.post("/_admin/chaos", json={"fail_status": 200}).status_code == 422


@pytest.mark.security
class TestSyntheticDataOnly:
    """Fail the build if anything that looks like real subscriber data appears."""

    MSISDN = re.compile(r"^\+447700900\d{3}$")  # Ofcom drama range
    IMSI = re.compile(r"^00101\d{10}$")  # MCC 001 / MNC 01 test network

    def test_msisdns_are_in_fictional_range(self):
        numbers = [s["msisdn"] for s in data.SUBSCRIPTIONS.values()]
        numbers += [a["contact_msisdn"] for a in data.ACCOUNTS.values()]
        assert all(self.MSISDN.fullmatch(n) for n in numbers), numbers

    def test_imsis_use_test_network(self):
        assert all(self.IMSI.fullmatch(s["imsi"]) for s in data.SUBSCRIPTIONS.values())

    def test_emails_use_reserved_tld(self):
        assert all(a["email"].endswith(".invalid") for a in data.ACCOUNTS.values())

    def test_postcodes_are_not_real(self):
        # ZZ is not a real UK postcode area.
        assert all(
            a["billing_address"]["postcode"].startswith("ZZ") for a in data.ACCOUNTS.values()
        )

    def test_injection_fixture_is_present(self):
        assert "ignore previous instructions" in data.ACCOUNTS["ACC-1001"]["notes"]
