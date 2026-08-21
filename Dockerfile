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

# Install the app, then remove pip itself. Nothing runs pip at runtime (the
# container starts uvicorn as a non-root user), and pip 26.2+ ships a vendored
# dependency set with its own CycloneDX SBOM at pip/_vendor/bom.cdx.json. Image
# scanners read that SBOM and report CVEs for the vendored copies (msgpack,
# setuptools) even though the app never imports them and they are unreachable in
# a running container. Dropping pip removes the package manager from the runtime
# image, which is worth doing on its own terms.
RUN pip install --upgrade pip && pip install . && python -m pip uninstall -y pip

RUN chown -R app:app /app

USER app

EXPOSE 38491

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:38491/healthz', timeout=3).read(); sys.exit(0)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "38491"]
