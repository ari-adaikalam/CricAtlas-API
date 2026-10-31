# syntax=docker/dockerfile:1
# The notifier image. Build from the repository root (api/deploy/local/build_release.sh does), for linux/amd64.
# The base image is pinned by digest (api/deploy/images.lock.json is the record, and a test holds this line equal to it).
ARG PYTHON_IMAGE=docker.io/library/python:3.13.16-slim-trixie@sha256:cf50a4f6ac4f026458b08d5222023d700ac806410dff5e9ca8728fa44f2b850f

FROM ${PYTHON_IMAGE} AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /src
# Third-party packages: only the exact files whose hashes are in requirements.txt, and only as wheels, so nothing is compiled or fetched from anywhere else.
COPY api/deploy/images/notifier/requirements.txt /src/requirements.txt
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --require-hashes --only-binary=:all: -r /src/requirements.txt
# The package itself is copied, not installed (a build backend would come from the network, outside the hashed set), and compiled here so that the running container never writes bytecode. Its templates come with it.
COPY api/notifier/cricket_notifier /app/cricket_notifier
RUN python -m compileall -q /app/cricket_notifier

FROM ${PYTHON_IMAGE}
ARG RELEASE_ID=unknown
LABEL org.opencontainers.image.title="cricket-notifier" org.opencontainers.image.revision="${RELEASE_ID}"
# A fixed, unprivileged user. The shared webhook token is read through a group that compose adds at run time (10011), not through this image.
RUN groupadd --gid 10003 cricket-notifier && useradd --uid 10003 --gid 10003 --no-create-home --shell /usr/sbin/nologin cricket-notifier
COPY --from=build /opt/venv /opt/venv
COPY --from=build /app /app
COPY api/deploy/images/notifier/entrypoint.sh /usr/local/bin/
RUN chmod 0555 /usr/local/bin/entrypoint.sh
ENV PATH=/opt/venv/bin:$PATH PYTHONPATH=/app PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONHASHSEED=random
USER 10003:10003
WORKDIR /tmp
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
