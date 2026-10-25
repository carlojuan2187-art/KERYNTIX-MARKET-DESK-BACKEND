# Reproducible Python 3.11 image for Railway or any OCI runtime.
FROM python:3.11-slim-bookworm AS dependency-base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements-prod.txt ./requirements-prod.txt
RUN python -m pip install --upgrade pip && \
    python -m pip install -r requirements-prod.txt && \
    useradd --create-home --uid 10001 ftrade

# Runtime image intentionally excludes tests, source archives and development dependencies.
FROM dependency-base AS runtime
COPY main.py ./main.py
COPY api ./api
COPY domain ./domain
COPY engine ./engine
COPY worker ./worker
RUN chown -R ftrade:ftrade /app
USER ftrade
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:' + __import__('os').getenv('PORT','8000') + '/live', timeout=3).read()" || exit 1
CMD ["/bin/sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]

# CI/integration image includes pytest and the tests but is never used for serving traffic.
FROM dependency-base AS test
COPY requirements-dev.txt ./requirements-dev.txt
RUN python -m pip install -r requirements-dev.txt
COPY . /app
RUN chown -R ftrade:ftrade /app
USER ftrade
CMD ["python", "-m", "pytest", "-q", "--disable-warnings"]
