"""A deliberately dumb round-robin HTTP load balancer, standing in for the
Cloud Foundry gorouter (no sticky sessions).

Each incoming request goes to the NEXT backend in turn, whatever its headers
say. Responses carry `X-Served-By: <backend port>` so you can watch requests
alternate between replicas.

    uv run python -m telco_mcp_lab.devtools.round_robin_lb --port 8099 \
        --backend http://127.0.0.1:8091 --backend http://127.0.0.1:8092

Streams responses (SSE-safe). Lab only: no timeouts tuning, no health checks.
"""

import argparse
import itertools
from collections.abc import AsyncIterator

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.routing import Route

_HOP_BY_HOP = {"connection", "keep-alive", "transfer-encoding", "te", "upgrade", "content-length"}


def build_app(backends: list[str]) -> Starlette:
    ring = itertools.cycle(backends)
    client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))

    async def proxy(request: Request) -> StreamingResponse:
        backend = next(ring)
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}
        upstream = client.build_request(
            request.method,
            backend + request.url.path,
            params=request.query_params,
            headers=headers,
            content=await request.body(),
        )
        resp = await client.send(upstream, stream=True)

        async def body() -> AsyncIterator[bytes]:
            async for chunk in resp.aiter_raw():
                yield chunk

        out_headers = {k: v for k, v in resp.headers.items() if k.lower() not in _HOP_BY_HOP}
        out_headers["X-Served-By"] = backend.rsplit(":", 1)[-1]
        return StreamingResponse(
            body(),
            status_code=resp.status_code,
            headers=out_headers,
            background=BackgroundTask(resp.aclose),
        )

    methods = ["GET", "POST", "DELETE", "OPTIONS"]
    return Starlette(routes=[Route("/{path:path}", proxy, methods=methods)])


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8099)
    p.add_argument("--backend", action="append", required=True)
    a = p.parse_args()
    uvicorn.run(build_app(a.backend), host="127.0.0.1", port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
