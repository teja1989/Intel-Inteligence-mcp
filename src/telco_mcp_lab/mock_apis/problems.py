"""Error responses as RFC 9457 Problem Details (`application/problem+json`).

Why this matters for MCP: the MCP server (Phase 3) translates these into
*actionable tool errors*. A stable machine-readable `code` makes that mapping
reliable, and there is no parsing of English messages.

Spring equivalent: `org.springframework.http.ProblemDetail` plus
`@RestControllerAdvice`, which emits the same JSON shape.
"""

from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

PROBLEM_JSON = "application/problem+json"


class ApiProblem(Exception):  # noqa: N818 - named after the RFC concept
    def __init__(self, status: int, code: str, detail: str, **extra: Any) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail
        self.extra = extra


def problem_response(status: int, code: str, detail: str, **extra: Any) -> JSONResponse:
    body = {
        "type": f"https://errors.telco-mcp-lab.invalid/{code.lower().replace('_', '-')}",
        "title": code.replace("_", " ").title(),
        "status": status,
        "detail": detail,
        "code": code,
        **extra,
    }
    return JSONResponse(body, status_code=status, media_type=PROBLEM_JSON)


async def api_problem_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiProblem)
    return problem_response(exc.status, exc.code, exc.detail, **exc.extra)


async def validation_problem_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    # Echo only the location and message. Never echo the input value back,
    # because it might contain PII or an injection payload.
    errors = [
        {"loc": [str(p) for p in e.get("loc", ())], "msg": e.get("msg", "")} for e in exc.errors()
    ]
    return problem_response(422, "VALIDATION_FAILED", "Request failed validation.", errors=errors)


async def http_problem_handler(_: Request, exc: Exception) -> JSONResponse:
    """Framework-level errors (unknown route, wrong method) in the same shape."""
    assert isinstance(exc, StarletteHTTPException)
    code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(exc.status_code, "HTTP_ERROR")
    return problem_response(exc.status_code, code, str(exc.detail))
