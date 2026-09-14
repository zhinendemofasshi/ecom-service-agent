FROM python:3.13-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    ECOM_ENV_FILE=""
WORKDIR /srv/ecom
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 ecom \
    && useradd --uid 10001 --gid ecom --no-create-home ecom
COPY app ./app
COPY prompts ./prompts
COPY schemas ./schemas
COPY config ./config
COPY agent ./agent
COPY tools ./tools
COPY mcp_client ./mcp_client
RUN mkdir -p app/sessions && chown -R ecom:ecom app/sessions
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"]
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--timeout-graceful-shutdown", "60", "--no-access-log"]
