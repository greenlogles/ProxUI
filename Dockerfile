FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY ./ /app/

# Create non-root user for security
RUN useradd -r -u 1000 -m -d /app -s /bin/bash proxui && \
    chown -R proxui:proxui /app

# Switch to non-root user
USER proxui

# Set environment variables
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# Expose web dashboard port
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8080/ || exit 1

# Default command
CMD ["python3", "app.py"]