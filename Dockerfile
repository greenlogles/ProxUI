FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt && \
    apt-get update && apt-get install -y --no-install-recommends su-exec && \
    rm -rf /var/lib/apt/lists/*

# Labels
LABEL maintainer="contact@proxui.app"
LABEL org.label-schema.name="ProxUI"
LABEL org.label-schema.description="ProxUI - A modern web-based management interface for Proxmox VE with multi-cluster support."
LABEL org.label-schema.vendor="greenlogles"
LABEL org.label-schema.url="https://proxui.app"
LABEL org.label-schema.vcs-url="https://github.com/greenlogles/proxui"
LABEL org.opencontainers.image.source="https://github.com/greenlogles/proxui"

# Copy application files
COPY ./static/ /app/static/
COPY ./templates/ /app/templates/
COPY ./app.py /app/
COPY ./cloud_images.json /app/
COPY ./entrypoint.sh /entrypoint.sh

# Create non-root user for security
RUN useradd -r -u 1000 -m -d /app -s /bin/bash proxui && \
    chown -R proxui:proxui /app && \
    chmod +x /entrypoint.sh

# Set environment variables
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
ENV CONFIG_FILE_PATH=/app/data/config.toml

# Expose web dashboard port
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8080/ || exit 1

# Entrypoint fixes data volume ownership at runtime, then drops to proxui user
ENTRYPOINT ["/entrypoint.sh"]
CMD ["python3", "app.py"]