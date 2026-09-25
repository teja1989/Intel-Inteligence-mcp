"""Identifier formats: the shared contract between the mock APIs and the MCP tool schemas.

Strict patterns are a cheap security control: an ID that can't match can't be
used for path traversal, SQL/log injection or smuggling instructions to a model.
"""

ACCOUNT_ID = r"^ACC-\d{4}$"
SUBSCRIPTION_ID = r"^SUB-\d{4}-\d{2}$"
SERVICE_ID = r"^SVC-\d{4}-\d{2}$"
ORDER_ID = r"^ORD-\d{6}$"
DRAFT_ID = r"^DRF-[0-9a-f]{32}$"
# Idempotency keys: 8-128 URL-safe characters (a UUID fits comfortably).
IDEMPOTENCY_KEY = r"^[A-Za-z0-9_-]{8,128}$"
