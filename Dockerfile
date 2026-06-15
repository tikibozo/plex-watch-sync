FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Pull patched OS packages (e.g. libssl3t64) from the Debian security repo. The
# base tag lags fresh CVE fixes between Docker's periodic rebuilds, so upgrade
# in place to keep the Trivy gate green.
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*

RUN groupadd -g 10001 -r app && useradd -u 10001 -r -g app -M -s /sbin/nologin app

WORKDIR /app

COPY pyproject.toml README.md /app/
COPY app/ /app/app/

RUN pip install --upgrade pip && pip install .

RUN chown -R app:app /app

USER app

EXPOSE 38491

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:38491/healthz', timeout=3).read(); sys.exit(0)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "38491"]
