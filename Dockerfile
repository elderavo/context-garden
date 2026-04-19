FROM python:3.12-slim

# Node is needed to run the mirror-cli subprocess
RUN apt-get update && apt-get install -y --no-install-recommends \
    nodejs npm git openssh-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps first (better layer cache)
COPY context_engine/requirements.txt ./context_engine/requirements.txt
RUN pip install --no-cache-dir -r context_engine/requirements.txt

# Node deps + build mirror
COPY package.json package-lock.json* ./
RUN npm ci || npm install
COPY tsconfig.json ./
COPY src/mirror ./src/mirror
RUN npx tsc && npm prune --omit=dev

# Application code
COPY context_engine ./context_engine

# Data volume — workspaces, config, and index files live here
VOLUME ["/data"]

ENV CG_WEBAPP_PORT=7433
EXPOSE 7433

CMD ["python", "-m", "context_engine", "--daemon", "--data-dir", "/data"]
