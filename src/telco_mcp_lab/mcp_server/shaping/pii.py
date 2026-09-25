"""PII masking. Masked by default; unmasked only for callers holding `pii:read`.

Why mask at all, when the caller is allowed to see their own data?
Everything a tool returns enters the LLM context: it may be logged by the host,
sent to the model provider, cached, echoed into later turns, or leaked by a
prompt injection. Minimise first, then let the few flows that need raw values
(e.g. a care agent confirming a number) opt in with an explicit scope.
"""

from telco_mcp_lab.mcp_server.security.caller import CallerContext, Scope


def mask_msisdn(msisdn: str) -> str:
    """+447700900111 -> +44*******111 (country code and last 3 digits kept)."""
    if len(msisdn) < 7:
        return "***"
    return msisdn[:3] + "*" * (len(msisdn) - 6) + msisdn[-3:]


def mask_name(name: str) -> str:
    """Alex Example -> A*** E****** (initials kept for recognisability)."""
    return " ".join(w[0] + "*" * (len(w) - 1) if w else w for w in name.split(" "))


class PiiPolicy:
    def __init__(self, caller: CallerContext) -> None:
        self.unmasked = caller.has(Scope.PII_READ)

    def msisdn(self, value: str) -> str:
        return value if self.unmasked else mask_msisdn(value)

    def name(self, value: str) -> str:
        return value if self.unmasked else mask_name(value)
