# EnterpriseMind API, dispatcher, migrator and worker image.
#
# The release pipeline must pass immutable image references for both build
# stages (for example, a digest-pinned python and uv image).  The defaults are
# intentionally absent so a production build cannot silently drift.
ARG PYTHON_IMAGE
ARG UV_IMAGE
FROM ${UV_IMAGE} AS uv
FROM ${PYTHON_IMAGE} AS runtime

COPY --from=uv /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1

WORKDIR /opt/enterprise-mind

# Keep the build context narrow and reproducible.  Tests, .venv, caches,
# evidence and local enterprise material are excluded by .dockerignore.
COPY backend/pyproject.toml backend/uv.lock ./backend/
COPY backend/migrations ./backend/migrations
COPY backend/alembic.ini ./backend/alembic.ini
COPY backend/src ./backend/src
COPY config ./config

RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/* \
    && uv sync --frozen --no-dev --project backend --no-cache \
    && addgroup --system --gid 65532 app \
    && adduser --system --uid 65532 --ingroup app app \
    && chown -R app:app /opt/enterprise-mind

USER 65532:65532

ENTRYPOINT ["/opt/enterprise-mind/backend/.venv/bin/python", "-m", "enterprise_mind.cli"]
