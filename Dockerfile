FROM python:3.12-slim

# Install Java and build tools (Debian Trixie compatible)
RUN apt-get update && apt-get install -y --no-install-recommends \
    default-jdk-headless \
    wget \
    unzip \
    git \
    file \
    && rm -rf /var/lib/apt/lists/*

# Install ADB from Google's platform tools
RUN wget -q -O /tmp/platform-tools.zip \
    https://dl.google.com/android/repository/platform-tools-latest-linux.zip \
    && unzip -q /tmp/platform-tools.zip -d /usr/local/ \
    && ln -s /usr/local/platform-tools/adb /usr/local/bin/adb \
    && rm /tmp/platform-tools.zip

# Install apktool
RUN wget -q -O /usr/local/bin/apktool.jar \
    https://github.com/iBotPeaches/Apktool/releases/download/v2.9.3/apktool_2.9.3.jar

# Install ew-cli (emulator.wtf CLI)
RUN wget -q -O /usr/local/bin/ew-cli \
    https://maven.emulator.wtf/releases/ew-cli \
    && chmod +x /usr/local/bin/ew-cli

# Install Python dependencies
RUN pip install --no-cache-dir \
    mcp starlette uvicorn httpx

# Create workspace
RUN mkdir -p /workspace
WORKDIR /workspace

# Copy MCP server
COPY mcp_server.py /app/mcp_server.py

EXPOSE 8080

CMD ["python", "/app/mcp_server.py"]
