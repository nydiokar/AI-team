# syntax=docker/dockerfile:1
ARG NODE_IMAGE=node:22-bookworm-slim
ARG PYTHON_IMAGE=python:3.11-slim-bookworm

FROM ${NODE_IMAGE} AS node-runtime

FROM node-runtime AS web-build
WORKDIR /build/web
RUN npm install --global pnpm@10.30.2
COPY web/package.json web/pnpm-lock.yaml web/pnpm-workspace.yaml ./
RUN --mount=type=cache,id=ai-team-pnpm,target=/pnpm/store \
    pnpm config set store-dir /pnpm/store \
    && pnpm install --frozen-lockfile
COPY web/ ./
RUN pnpm build

FROM ${PYTHON_IMAGE} AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH=/opt/venv/bin:$PATH
WORKDIR /app
RUN apt-get update \
    && apt-get install --no-install-recommends -y git tini curl \
    && rm -rf /var/lib/apt/lists/* \
    && python -m venv /opt/venv
COPY pyproject.toml constraints.txt ./
COPY src/ ./src/
COPY config/ ./config/
COPY __init__.py ./
RUN --mount=type=cache,id=ai-team-pip,target=/root/.cache/pip \
    pip install -c constraints.txt .
COPY main.py server_main.py worker_main.py ./
COPY scripts/ ./scripts/
COPY deploy/docker-entrypoint.sh /usr/local/bin/docker-entrypoint
COPY --from=web-build /build/web/dist ./web/dist
RUN groupadd --gid 10001 ai-team \
    && useradd --uid 10001 --gid ai-team --home-dir /app --shell /usr/sbin/nologin ai-team \
    && mkdir -p state logs tasks results summaries \
    && chown -R ai-team:ai-team /app \
    && chmod 755 /usr/local/bin/docker-entrypoint
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/docker-entrypoint"]
CMD ["python", "main.py"]

FROM runtime AS worker-agents
COPY --from=node-runtime /usr/local /usr/local
# Coding-agent runtimes are pinned as build ARGs so Renovate can bump them via
# PR (datasource=npm) rather than editing a RUN line by hand. These are the
# single source of truth for the requested versions; the acceptance harness
# asserts requested (this ARG) vs actual (installed) at runtime. Do NOT
# reintroduce a `RUN npm install <pkg>@<literal>` — that breaks Renovate's
# ability to track the version and the requested-vs-actual assertion.
# renovate: datasource=npm depName=pnpm
ARG PNPM_VERSION=10.30.2
# renovate: datasource=npm depName=@anthropic-ai/claude-code
ARG CLAUDE_CODE_VERSION=2.1.281
# renovate: datasource=npm depName=@openai/codex
ARG CODEX_VERSION=0.156.1
# Build-time git SHA of the checkout the image was built from (image identity
# for the acceptance inventory). Optional; defaults to "unknown" for a plain
# `docker build` without the harness.
ARG AI_TEAM_GIT_SHA=unknown
# Recorded into the image so the running container can report requested versions
# and its own identity without needing the build context (the harness reads these).
ENV AI_TEAM_REQUESTED_CODEX_VERSION=${CODEX_VERSION} \
    AI_TEAM_REQUESTED_CLAUDE_CODE_VERSION=${CLAUDE_CODE_VERSION} \
    AI_TEAM_REQUESTED_PNPM_VERSION=${PNPM_VERSION} \
    AI_TEAM_GIT_SHA=${AI_TEAM_GIT_SHA}
RUN npm install --global --omit=dev \
        pnpm@${PNPM_VERSION} \
        @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} \
        @openai/codex@${CODEX_VERSION} \
    && chown -R ai-team:ai-team /usr/local/lib/node_modules
CMD ["python", "worker_main.py"]
