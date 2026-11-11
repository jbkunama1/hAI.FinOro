FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY app.py .
COPY app/ app/

# Run as unprivileged user
RUN useradd --create-home --uid 1000 appuser && chown -R appuser:appuser /app
USER appuser

# config.json is mounted via docker-compose volume (never baked into image)
# Host file must be readable (chmod 644)
VOLUME ["/app/config.json"]

EXPOSE 5000

# Use gunicorn in production
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "120", "app:app"]
