# syntax=docker/dockerfile:1
ARG NODE_IMAGE=node:22-bookworm-slim
ARG PYTHON_IMAGE=python:3.11-slim-bookworm

FROM ${NODE_IMAGE} AS web-build
WORKDIR /build/web
RUN npm install --global pnpm@10.30.2
COPY web/package.json web/pnpm-lock.yaml web/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
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
RUN pip install -c constraints.txt .
COPY main.py server_main.py worker_main.py ./
COPY scripts/ ./scripts/
COPY --from=web-build /build/web/dist ./web/dist
RUN groupadd --gid 10001 ai-team \
    && useradd --uid 10001 --gid ai-team --home-dir /app --shell /usr/sbin/nologin ai-team \
    && mkdir -p state logs tasks results summaries \
    && chown -R ai-team:ai-team /app
USER ai-team
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "main.py"]

FROM runtime AS worker-codex
USER root
COPY --from=web-build /usr/local/bin/node /usr/local/bin/node
COPY --from=web-build /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && npm install --global --omit=dev @openai/codex@0.153.2 \
    && chown -R ai-team:ai-team /usr/local/lib/node_modules
USER ai-team
CMD ["python", "worker_main.py"]
