FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=5000 \
    GUNICORN_WORKERS=2 \
    GUNICORN_TIMEOUT=300

WORKDIR /app

# System deps for healthcheck and SSL root certs
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

# Copy sources first to leverage Docker cache
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

EXPOSE 5000

# Simple healthcheck hitting /health
HEALTHCHECK --interval=30s --timeout=5s --retries=5 \
  CMD sh -c "curl -fsS http://127.0.0.1:${PORT}/health >/dev/null || exit 1"

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:5000", "--timeout", "300", "--access-logfile", "-", "claude_proxy:app"]

