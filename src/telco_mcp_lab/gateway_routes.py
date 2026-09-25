"""Gateway path layout, shared by the mock gateway (server side) and the MCP
server's gateway client (caller side).

Every domain call goes through the API gateway as:

    {GATEWAY_BASE_URL}/{microservice}/{GATEWAY_API_SEGMENT}/{resource...}
    e.g. http://127.0.0.1:8081/bosubscription/API/subscription/SUB-1001-01

The microservice names below are PLACEHOLDERS. Override them in `.env`
(GATEWAY_SVC_*) to match your organisation's naming. Both sides read the same
variables, so they can't drift apart.
"""

from enum import StrEnum

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_SEGMENT = r"^[A-Za-z0-9_-]{1,64}$"  # one safe path segment, no slashes or dots


class DomainApi(StrEnum):
    ACCOUNT = "account"
    SUBSCRIPTION = "subscription"
    SERVICE = "service"
    ORDER = "order"
    ORDER_SUBMISSION = "order_submission"


class GatewayRoutes(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GATEWAY_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    api_segment: str = Field(default="API", pattern=_SEGMENT)
    svc_account: str = Field(default="boaccount", pattern=_SEGMENT)
    svc_subscription: str = Field(default="bosubscription", pattern=_SEGMENT)
    svc_service: str = Field(default="boservice", pattern=_SEGMENT)
    svc_order: str = Field(default="boorder", pattern=_SEGMENT)
    svc_order_submission: str = Field(default="boordersubmission", pattern=_SEGMENT)

    def prefix(self, api: DomainApi) -> str:
        """`/{microservice}/{api_segment}`, e.g. `/bosubscription/API`."""
        return f"/{getattr(self, f'svc_{api.value}')}/{self.api_segment}"
