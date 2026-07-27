#!/bin/sh
set -e
echo "=== Redeploy v14.0-powerhouse ==="
echo "Downloading latest code..."
wget -q -O /tmp/repo.tar.gz https://github.com/CypherNitro/zerops-mcp-server/archive/refs/heads/main.tar.gz
tar xzf /tmp/repo.tar.gz -C /tmp/
echo "Building Docker image (this takes 3-5 min)..."
docker build --no-cache -t ar-mcp:v140 /tmp/zerops-mcp-server-main
echo "Stopping old container..."
docker stop $(docker ps -q --filter ancestor=android-re-mcp) 2>/dev/null || true
docker stop $(docker ps -q --filter ancestor=android-re-mcp:v6) 2>/dev/null || true
docker stop $(docker ps -q --filter ancestor=android-re-mcp:v11) 2>/dev/null || true
docker stop $(docker ps -q --filter ancestor=ar-mcp:v140) 2>/dev/null || true
echo "Starting new container..."
docker run -d --network=host -e PORT=8080 -e EW_API_TOKEN=$EW_API_TOKEN ar-mcp:v140
echo ""
echo "=== DONE! ==="
echo "Testing health..."
sleep 3
curl -s http://localhost:8080/health
echo ""
echo "Testing SSE HEAD..."
curl -s -o /dev/null -w "%{http_code}" -I http://localhost:8080/sse
echo ""
echo "Testing OAuth protected resource..."
curl -s http://localhost:8080/.well-known/oauth-protected-resource/sse
echo ""
