"""Test helper: a deliberately dumb round-robin HTTP load balancer, standing in for the
Cloud Foundry gorouter / a Kubernetes Service (no sticky sessions).

Each request goes to the NEXT backend in turn. Responses carry `X-Served-By: <backend
port>` so tests can prove requests were spread over replicas. Streams responses.
"""

import itertools
from collections.abc import AsyncIterator

import httpx
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
