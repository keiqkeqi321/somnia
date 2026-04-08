# =============================================================
#  Somnia — Dockerfile
#  用法:
#    docker build -t somnia .
#    docker run -it somnia
#    docker run -it somnia chat "hello"
# =============================================================
FROM python:3.12-slim

LABEL org.opencontainers.image.title="Somnia"
LABEL org.opencontainers.image.description="A modular AI agent CLI — somnia (Latin for 'dreams')"
LABEL org.opencontainers.image.source="https://github.com/your-org/openagent"

# Install system deps
RUN apt-get update && \
    apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

# Copy and install
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir .

# Default entrypoint
ENTRYPOINT ["somnia"]
CMD []
