"""Service API: technical service details for a subscription (SIM, features)."""

from typing import Annotated

from fastapi import APIRouter, Path

from telco_mcp_lab import ids
from telco_mcp_lab.mock_apis import data
from telco_mcp_lab.mock_apis.problems import ApiProblem

router = APIRouter(prefix="/service", tags=["Service API"])


@router.get("/{service_id}")
def get_service(service_id: Annotated[str, Path(pattern=ids.SERVICE_ID)]) -> dict:
    svc = data.SERVICES.get(service_id)
    if svc is None:
        raise ApiProblem(404, "SERVICE_NOT_FOUND", f"No service with id {service_id}.")
    return svc
