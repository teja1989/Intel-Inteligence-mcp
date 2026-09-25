"""FastAPI application that stands in for the API gateway + telecom backends.

It mimics the gateway's routing: every domain API lives under
`/{microservice}/API/...` (see `gateway_routes.py`), e.g.

    GET /bosubscription/API/subscription/SUB-1001-01
    Authorization: Bearer <GATEWAY_TOKEN>

One process hosts all five APIs, so a single `make mocks` starts everything.
The MCP server addresses them as five separate microservices, as it will in
production.

Run:  uv run python -m telco_mcp_lab.mock_apis      (or: make mocks)
Docs: http://127.0.0.1:8081/docs                     (OpenAPI UI)
"""

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from telco_mcp_lab.gateway_routes import DomainApi, GatewayRoutes
from telco_mcp_lab.mock_apis.chaos import ChaosConfig, ChaosMiddleware, TrailingSlashMiddleware
from telco_mcp_lab.mock_apis.deps import require_gateway_token
from telco_mcp_lab.mock_apis.problems import (
    ApiProblem,
    api_problem_handler,
    http_problem_handler,
    validation_problem_handler,
)
from telco_mcp_lab.mock_apis.routers import accounts, orders, services, submissions, subscriptions
from telco_mcp_lab.mock_apis.settings import MockApiSettings
from telco_mcp_lab.mock_apis.store import Clock, OrderStore, utc_now


class ChaosUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    delay_ms: int = Field(default=0, ge=0, le=60_000)
    fail_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    fail_status: int = Field(default=503, ge=500, le=599)


def create_app(
    settings: MockApiSettings | None = None,
    routes: GatewayRoutes | None = None,
    clock: Clock = utc_now,
) -> FastAPI:
    settings = settings or MockApiSettings()  # type: ignore[call-arg]  # filled from env
    routes = routes or GatewayRoutes()
    app = FastAPI(
        title="Telco Mock Gateway",
        description="Synthetic Account/Subscription/Service/Order/Submission APIs. No real data.",
        version="0.1.0",
    )
    app.state.settings = settings
    app.state.store = OrderStore(settings.db_path, settings.draft_ttl_seconds, clock)
    app.state.chaos = ChaosConfig(
        settings.chaos_delay_ms, settings.chaos_fail_rate, settings.chaos_fail_status
    )

    app.add_middleware(ChaosMiddleware)
    app.add_middleware(TrailingSlashMiddleware)  # outermost: runs before routing
    app.add_exception_handler(ApiProblem, api_problem_handler)
    app.add_exception_handler(RequestValidationError, validation_problem_handler)
    app.add_exception_handler(StarletteHTTPException, http_problem_handler)

    authed = [Depends(require_gateway_token)]
    for api, module in (
        (DomainApi.ACCOUNT, accounts),
        (DomainApi.SUBSCRIPTION, subscriptions),
        (DomainApi.SERVICE, services),
        (DomainApi.ORDER, orders),
        (DomainApi.ORDER_SUBMISSION, submissions),
    ):
        app.include_router(module.router, prefix=routes.prefix(api), dependencies=authed)

    @app.get("/health", tags=["Ops"])
    def health() -> dict:
        return {"status": "ok"}

    if settings.admin_enabled:

        @app.get("/_admin/chaos", tags=["Ops"], dependencies=authed)
        def get_chaos(request: Request) -> dict:
            return vars(request.app.state.chaos)

        @app.post("/_admin/chaos", tags=["Ops"], dependencies=authed)
        def set_chaos(update: ChaosUpdate, request: Request) -> dict:
            request.app.state.chaos = ChaosConfig(**update.model_dump())
            return vars(request.app.state.chaos)

    return app
