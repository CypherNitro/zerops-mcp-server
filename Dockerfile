FROM python:3.12-slim

# Install Java JDK, ADB, and build tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    openjdk-17-jdk-headless \
    android-tools-adb \
    wget \
    unzip \
    git \
    file \
    && rm -rf /var/lib/apt/lists/*

# Install apktool
RUN wget -q -O /usr/local/bin/apktool.jar \
    https://github.com/iBotPeaches/Apktool/releases/download/v2.9.3/apktool_2.9.3.jar

# Install ew-cli (emulator.wtf CLI)
RUN wget -q -O /usr/local/bin/ew-cli \
    https://maven.emulator.wtf/releases/ew-cli \
    && chmod +x /usr/local/bin/ew-cli

# Install Python dependencies
RUN pip install --no-cache-dir \
    mcp starlette uvicorn httpx frida-tools

# Create workspace
RUN mkdir -p /workspace
WORKDIR /workspace

# Copy MCP server
COPY mcp_server.py /app/mcp_server.py

EXPOSE 8080

CMD ["python", "/app/mcp_server.py"]
