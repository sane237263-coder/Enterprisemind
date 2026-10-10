ARG PYTHON_IMAGE
ARG UV_IMAGE
FROM ${UV_IMAGE} AS uv
FROM ${PYTHON_IMAGE} AS runtime
RUN test "$(dpkg --print-architecture)" = amd64 \
    || { echo 'Office converter currently supports Linux amd64 only' >&2; exit 1; }
ARG LIBREOFFICE_SHA256=fd0e8f8f2408dd2e5b90286e60f3f97cf566ba441cd48cfc5bcc68067303e0bc
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl fonts-noto-cjk libcairo2 libcups2 libdbus-1-3 libxinerama1 libxrender1 libsm6 \
    && rm -rf /var/lib/apt/lists/*
RUN curl --fail --location --silent --show-error --retry 3 --retry-all-errors \
       --connect-timeout 30 --max-time 600 --proto '=https' --proto-redir '=https' \
       https://download.documentfoundation.org/libreoffice/stable/26.2.6/deb/x86_64/LibreOffice_26.2.6_Linux_x86-64_deb.tar.gz \
       -o /tmp/libreoffice.tar.gz \
    && echo "$LIBREOFFICE_SHA256  /tmp/libreoffice.tar.gz" | sha256sum -c - \
    && mkdir /tmp/libreoffice \
    && tar -xzf /tmp/libreoffice.tar.gz -C /tmp/libreoffice \
    && apt-get install -y --no-install-recommends /tmp/libreoffice/*/DEBS/*.deb \
    && ln -s /opt/libreoffice26.2/program/soffice /usr/bin/libreoffice \
    && rm -rf /tmp/libreoffice /tmp/libreoffice.tar.gz /var/lib/apt/lists/*
RUN apt-get update && apt-get install -y --no-install-recommends libnss3 libglib2.0-0 libx11-xcb1 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock ./backend/
COPY backend/src ./backend/src
COPY --from=uv /uv /usr/local/bin/uv
RUN UV_PYTHON_DOWNLOADS=never uv sync --project backend --frozen --no-dev --no-cache
USER 65532:65532
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp XDG_CACHE_HOME=/tmp/.cache
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD ["/app/backend/.venv/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health/ready', timeout=8).close()"]
CMD ["/app/backend/.venv/bin/python", "-m", "uvicorn", "enterprise_mind.office_converter:factory", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
