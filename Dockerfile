# Remote: clock, Claude Code sessions and Spotify for a phone on the home network.
#   runtime (default)  the server, non-root, data in /data
#   test               the server plus pytest and Chromium: runs scripts/test.sh
FROM python:3.13-slim AS deps
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_ROOT_USER_ACTION=ignore
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

FROM deps AS test
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright HOME=/tmp
COPY requirements-dev.txt .
RUN apt-get update && apt-get install -y --no-install-recommends libatomic1 && pip install -r requirements-dev.txt \
 && playwright install --with-deps chromium && rm -rf /var/lib/apt/lists/*
COPY server ./server
COPY web ./web
COPY hooks ./hooks
COPY tests ./tests
COPY scripts ./scripts
USER 1000:1000
ENTRYPOINT ["sh", "scripts/test.sh"]

FROM deps AS runtime
COPY server ./server
COPY web ./web
COPY hooks ./hooks
ENV REMOTE_DATA=/data
USER 1000:1000
EXPOSE 8796
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8796/health',timeout=3)"
CMD ["python", "-m", "server", "--host", "0.0.0.0", "--port", "8796"]
