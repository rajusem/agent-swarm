FROM python:3.12-slim

WORKDIR /app

# Install kubectl (used by TUI WebSocket proxy for kubectl exec into session pods)
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && \
    KUBECTL_VERSION=$(curl -sSL https://dl.k8s.io/release/stable.txt) && \
    curl -sSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" -o /usr/local/bin/kubectl && \
    chmod +x /usr/local/bin/kubectl && \
    apt-get remove -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

# Install dependencies first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY swarmer/ swarmer/

# Directories for mounted volumes (PVC for DB, Secret for auth hash)
RUN mkdir -p /data /auth

ENV PYTHONUNBUFFERED=1 \
    K8S_IN_CLUSTER=true \
    AUTH_HASH_FILE=/auth/password.hash \
    DATABASE_URL=sqlite+aiosqlite:////data/swarmer.db

EXPOSE 8080

CMD ["uvicorn", "swarmer.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips=*"]
