# Minimal experiment image. NuRouter/AISW is mounted read-only at run time;
# credentials and coordinator configuration never enter this image.
FROM node:22-bookworm-slim

ARG PI_VERSION=0.84.2
ARG CODEX_VERSION=0.148.0
ARG CONTEXTSWARM_SOURCE_COMMIT=unknown
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/opt/contextswarm \
    CONTEXTSWARM_REPO_ROOT=/opt/contextswarm \
    CONTEXTSWARM_SOURCE_COMMIT=${CONTEXTSWARM_SOURCE_COMMIT}

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 ca-certificates git bash procps \
    && rm -rf /var/lib/apt/lists/* \
    && npm install --global "@earendil-works/pi-coding-agent@${PI_VERSION}" "@openai/codex@${CODEX_VERSION}" \
    && node --version \
    && pi --version

# The launcher normally overrides this identity with the invoking host UID:GID
# so bind-mounted run artifacts remain owned by the operator.  Keep a non-root
# image default as a safe fallback for direct docker invocations.
RUN groupadd --system --gid 65532 contextswarm \
    && useradd --system --uid 65532 --gid 65532 --home-dir /run/contextswarm-mini/home \
        --no-create-home --shell /usr/sbin/nologin contextswarm \
    && install -d -o 65532 -g 65532 -m 0700 /run/contextswarm-mini \
    && install -d -m 0755 \
        /opt/contextswarm-input/aisw \
        /opt/contextswarm-input/aisw-private \
        /opt/contextswarm-input/codex-home \
    && touch \
        /opt/contextswarm-input/aisw/pi \
        /opt/contextswarm-input/aisw-private/node.toml

WORKDIR /opt/contextswarm
COPY . /opt/contextswarm

RUN python3 -m compileall -q contextswarm_mini

COPY docker-entrypoint.sh /usr/local/bin/contextswarm-mini-entrypoint
RUN chmod 0755 /usr/local/bin/contextswarm-mini-entrypoint

USER 65532:65532
ENTRYPOINT ["/usr/local/bin/contextswarm-mini-entrypoint"]
CMD ["--config", "configs/cps.toml", "run"]
