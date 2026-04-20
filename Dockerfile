# syntax=docker/dockerfile:1

# ── Stage 1: compile TypeScript mirror ──────────────────────────────────────
FROM node:20-slim AS ts-build
WORKDIR /build
COPY package.json package-lock.json* ./
RUN --mount=type=cache,target=/root/.npm \
    npm ci
COPY tsconfig.json ./
COPY src/mirror ./src/mirror
RUN npx tsc && npm prune --omit=dev

# ── Stage 2: Python runtime ──────────────────────────────────────────────────
FROM python:3.12-slim

# git + ssh for workspace cloning; nodejs (no npm) to run mirror-cli at runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
    git openssh-client nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps — cached between builds as long as requirements.txt unchanged
COPY context_engine/requirements.txt ./context_engine/requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r context_engine/requirements.txt

# Compiled mirror JS + runtime node_modules (typescript used as library)
COPY --from=ts-build /build/dist ./dist
COPY --from=ts-build /build/node_modules ./node_modules

# Application code (changes most often — last layer)
COPY context_engine ./context_engine
COPY src/data ./src/data

VOLUME ["/data"]
ENV CG_WEBAPP_PORT=7433
EXPOSE 7433

CMD ["python", "-m", "context_engine", "--daemon", "--data-dir", "/data"]
