#!/bin/sh
set -e
echo "=== Setup v5 ==="

# Determine privilege escalation
if command -v sudo >/dev/null 2>&1; then
    PRIV="sudo"
elif command -v doas >/dev/null 2>&1; then
    PRIV="doas"
else
    PRIV=""
fi

echo "Using: $PRIV"

# Install Python and tools
$PRIV apk add --no-cache python3 py3-pip wget tar 2>/dev/null || apk add --no-cache python3 py3-pip wget tar 2>/dev/null || echo "apk failed, trying existing python3"

# Install Python packages
pip3 install --break-system-packages mcp starlette uvicorn httpx 2>/dev/null || $PRIV pip3 install --break-system-packages mcp starlette uvicorn httpx

echo "=== Downloading code ==="
rm -rf /tmp/zerops-mcp-server-main /tmp/repo.tar.gz
wget -O /tmp/repo.tar.gz https://github.com/CypherNitro/zerops-mcp-server/archive/refs/heads/main.tar.gz
tar xzf /tmp/repo.tar.gz -C /tmp/

mkdir -p /workspace /app
cp /tmp/zerops-mcp-server-main/mcp_server.py /app/mcp_server.py

echo "=== Downloading tools ==="
$PRIV wget -q -O /usr/local/bin/apktool.jar https://github.com/iBotPeaches/Apktool/releases/download/v2.9.3/apktool_2.9.3.jar 2>/dev/null || wget -q -O /usr/local/bin/apktool.jar https://github.com/iBotPeaches/Apktool/releases/download/v2.9.3/apktool_2.9.3.jar
$PRIV wget -q -O /usr/local/bin/ew-cli https://maven.emulator.wtf/releases/ew-cli 2>/dev/null || wget -q -O /usr/local/bin/ew-cli https://maven.emulator.wtf/releases/ew-cli
$PRIV chmod +x /usr/local/bin/ew-cli 2>/dev/null || chmod +x /usr/local/bin/ew-cli

echo "=== Setup complete ==="
python3 -c "import mcp; print('mcp version:', mcp.__version__)"
