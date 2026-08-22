# Minimal experiment image. NuRouter/AISW is mounted read-only at run time;
# credentials and coordinator configuration never enter this image.
FROM node:22-bookworm-slim

ARG PI_VERSION=0.84.2
ARG CODEX_VERSION=0.148.0
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/opt/contextswarm \
    CONTEXTSWARM_REPO_ROOT=/opt/contextswarm \
    CONTEXTSWARM_MINI_PI_VERSION=${PI_VERSION} \
    CONTEXTSWARM_MINI_CODEX_VERSION=${CODEX_VERSION}

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 ca-certificates git bash procps \
    && rm -rf /var/lib/apt/lists/* \
    && npm install --global "@earendil-works/pi-coding-agent@${PI_VERSION}" "@openai/codex@${CODEX_VERSION}" \
    && node --version \
    && pi --version

WORKDIR /opt/contextswarm
COPY . /opt/contextswarm

RUN python3 -m compileall -q contextswarm_mini

COPY docker-entrypoint.sh /usr/local/bin/contextswarm-mini-entrypoint
RUN chmod 0755 /usr/local/bin/contextswarm-mini-entrypoint

ENTRYPOINT ["/usr/local/bin/contextswarm-mini-entrypoint"]
CMD ["--config", "configs/cps.toml", "run"]
