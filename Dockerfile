# ABUTRON QUANT - dependency-free by design (Python standard library only).
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APEX_HOME=/app

WORKDIR /app

COPY pyproject.toml README.md ./
COPY apex ./apex
COPY config ./config
COPY docs ./docs

# Runtime state lives outside the image so a restart keeps the journal and the
# risk/prop state (see docs/LIVE_DEPLOYMENT.md, "restart safety").
RUN mkdir -p /app/runs /app/data && useradd -m -u 10001 apex && chown -R apex /app
USER apex

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status==200 else 1)"

# Safe default: paper mode with the synthetic tape. Live trading needs --live,
# credentials and a demo account that has produced broker-confirmed fills.
ENTRYPOINT ["python", "-m", "apex"]
CMD ["serve", "--demo", "--host", "0.0.0.0", "--port", "8000"]
