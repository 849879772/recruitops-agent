FROM node:22-bookworm-slim AS codex-cli

ARG NPM_CONFIG_REGISTRY=https://registry.npmjs.org
RUN mkdir -p /opt/codex-cli \
    && npm install \
        --prefix /opt/codex-cli \
        --registry "${NPM_CONFIG_REGISTRY}" \
        --no-save \
        --no-package-lock \
        --no-fund \
        --no-audit \
        @openai/codex@0.149.0 \
    && node -e 'const p=require("/opt/codex-cli/node_modules/@openai/codex/package.json"); if (p.version !== "0.149.0") process.exit(1)'

FROM python:3.11-slim-bookworm

ARG PIP_INDEX_URL=https://pypi.org/simple
ARG DEBIAN_MIRROR=deb.debian.org
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL}

WORKDIR /app

RUN cp /etc/apt/sources.list.d/debian.sources /tmp/debian.sources \
    && sed -i "s|http://|https://|g; s|deb.debian.org|${DEBIAN_MIRROR}|g; s|security.debian.org|${DEBIAN_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
    && (apt-get -o Acquire::Retries=5 update || (cp /tmp/debian.sources /etc/apt/sources.list.d/debian.sources && sed -i "s|http://|https://|g" /etc/apt/sources.list.d/debian.sources && apt-get -o Acquire::Retries=5 update)) \
    && apt-get -o Acquire::Retries=5 install -y --no-install-recommends chromium libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/* /tmp/debian.sources

COPY --from=codex-cli /usr/local/bin/node /usr/local/bin/node
COPY --from=codex-cli /opt/codex-cli /opt/codex-cli
RUN node -e 'const p=require("/opt/codex-cli/node_modules/@openai/codex/package.json"); if (p.version !== "0.149.0") process.exit(1)'

ENV CODEX_HOME=/app/.data/codex-home \
    RECRUITOPS_BROWSER_CHANNEL=chromium \
    RECRUITOPS_BROWSER_EXECUTABLE_PATH=/usr/bin/chromium \
    PATH=/opt/codex-cli/node_modules/.bin:${PATH} \
    RECRUITOPS_CODEX_CLI_VERSION=0.149.0

COPY pyproject.toml ./
RUN --mount=type=cache,target=/root/.cache/pip \
    python -c "import subprocess,sys,tomllib; d=tomllib.load(open('pyproject.toml','rb')); subprocess.check_call([sys.executable,'-m','pip','install',*d['build-system']['requires'],*d['project']['dependencies']])"
COPY README.md ./
COPY apps ./apps
COPY packages ./packages
COPY config ./config
COPY AGENTS.md ./AGENTS.md
COPY .agents ./.agents
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install . --no-deps --no-build-isolation

COPY evals ./evals
COPY migrations ./migrations
COPY scripts ./scripts

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin app \
    && mkdir -p /app/.data \
    && chown -R app:app /app /opt/codex-cli

USER app

EXPOSE 8010

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8010/ready', timeout=3)"

CMD ["sh", "-c", "python scripts/apply_migrations.py && exec uvicorn apps.api.main:app --host 0.0.0.0 --port 8010"]
