FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install python dependencies
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

# Copy source code and frontend assets
COPY backend /app/backend

# Create directory for persistent data and logs
RUN mkdir -p /app/data /app/logs

# Environment configurations default to persistent volume
ENV PYTHONUNBUFFERED=1
ENV DB_PATH=/app/data/bstocks.db

EXPOSE 8000

CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
