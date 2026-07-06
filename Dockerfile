FROM python:3.12-slim

# Prevent Python from writing .pyc files and enable unbuffered logging
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/src

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create data directory and set permissions
RUN mkdir -p /data && chown -R nobody:nogroup /data /app

# Copy package management files first to leverage Docker cache
COPY pyproject.toml README.md ./

# Install the dependencies
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -e .

# Copy the rest of the application
COPY src/ ./src/

# Set ownership of all copied files to nobody
RUN chown -R nobody:nogroup /app

# Expose port 8000 for the web dashboard API
EXPOSE 8000

# Declare volume for persistence
VOLUME ["/data"]

# Set the non-root user
USER nobody

# Healthcheck configuration
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD curl -f http://localhost:8000/health || exit 1

# Run the web dashboard API by default
CMD ["python", "-m", "agent.interfaces.web"]
