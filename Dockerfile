# syntax=docker/dockerfile:1
# telco-mcp-server image: the MCP server ONLY (server/). Nothing from lab/ gets in.
# Same image for Cloud Foundry (docker push) and EKS. Config comes from environment
# variables; the client registry from /app/config (mount the reviewed production file).
#
#   make docker-build            → telco-mcp-server:dev
#   make docker-run              → runs it against the local mocks (dev keys)

ARG PYTHON_IMAGE=python:3.12-slim-bookworm

# ---- build: resolve and install locked dependencies into /app/.venv
FROM ${PYTHON_IMAGE} AS build
# Behind a TLS-intercepting proxy, pass its root CA as a BUILD SECRET (never baked into
# a layer):  docker build --secret id=corp_ca,src=/path/corp-root.pem \
#            --build-arg HTTPS_PROXY=... --build-arg HTTP_PROXY=... .
# uv from PyPI (pinned) rather than ghcr.io: one registry fewer to allow-list.
RUN --mount=type=secret,id=corp_ca,required=false \
    if [ -s /run/secrets/corp_ca ]; then export PIP_CERT=/run/secrets/corp_ca; fi; \
    pip install --no-cache-dir uv==0.8.17
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PYTHON=python3.12
WORKDIR /app
# Workspace metadata only (lab/ is a workspace member, so its pyproject must exist
# for the lock to resolve; its code is never copied).
COPY pyproject.toml uv.lock ./
COPY server/pyproject.toml server/pyproject.toml
COPY lab/pyproject.toml lab/pyproject.toml
RUN --mount=type=secret,id=corp_ca,required=false \
    if [ -s /run/secrets/corp_ca ]; then export SSL_CERT_FILE=/run/secrets/corp_ca; fi; \
    uv sync --frozen --no-dev --package telco-mcp-server --no-install-workspace
COPY server/src server/src
RUN uv sync --frozen --no-dev --package telco-mcp-server --no-editable

# ---- runtime: no uv, no compilers, no source tree, non-root
FROM ${PYTHON_IMAGE}
RUN useradd --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin mcp
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY server/config/clients.json /app/config/clients.json
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MCP_HOST=0.0.0.0 \
    MCP_LOG_FORMAT=json \
    MCP_CLIENTS_CONFIG=/app/config/clients.json
USER 10001:10001
EXPOSE 8090
# PORT (Cloud Foundry) or MCP_PORT sets the port. No shell: signals reach the server,
# so SIGTERM drains in-flight requests (MCP_SHUTDOWN_GRACE_S).
ENTRYPOINT ["telco-mcp", "--transport", "http"]
