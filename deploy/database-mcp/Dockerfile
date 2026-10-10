ARG PYTHON_IMAGE
ARG UV_IMAGE
FROM ${UV_IMAGE} AS uv
FROM ${PYTHON_IMAGE} AS runtime
COPY --from=uv /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /opt/enterprise-mind

COPY backend/pyproject.toml backend/uv.lock ./backend/
COPY backend/src ./backend/src
RUN uv sync --frozen --no-dev --project backend --no-cache

RUN addgroup --system --gid 65532 app \
    && adduser --system --uid 65532 --ingroup app app \
    && chown -R app:app /opt/enterprise-mind
USER 65532:65532

EXPOSE 8000
ENTRYPOINT ["/opt/enterprise-mind/backend/.venv/bin/python", "-m", "enterprise_mind.database.mcp_server"]
