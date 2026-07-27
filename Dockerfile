FROM python:3.12-slim

# ---- system tools ----
# zip/curl/binutils/vim-common were repeatedly missing at runtime; bake them in.
RUN apt-get update && apt-get install -y --no-install-recommends \
    default-jdk-headless \
    wget \
    curl \
    unzip \
    zip \
    git \
    file \
    binutils \
    vim-common \
    procps \
    && rm -rf /var/lib/apt/lists/*

# ---- ADB (Google platform tools) ----
RUN wget -q -O /tmp/platform-tools.zip \
    https://dl.google.com/android/repository/platform-tools-latest-linux.zip \
    && unzip -q /tmp/platform-tools.zip -d /usr/local/ \
    && ln -s /usr/local/platform-tools/adb /usr/local/bin/adb \
    && rm /tmp/platform-tools.zip

# ---- Android build-tools r34: zipalign / apksigner / aapt2 ----
# Needed to align, sign and inspect APKs (and to co-sign split sets).
RUN mkdir -p /opt/android \
    && wget -q -O /tmp/bt.zip https://dl.google.com/android/repository/build-tools_r34-linux.zip \
    && unzip -q /tmp/bt.zip -d /opt/android \
    && rm /tmp/bt.zip \
    && for b in zipalign apksigner aapt2 aapt d8; do \
         [ -e /opt/android/android-14/$b ] && ln -sf /opt/android/android-14/$b /usr/local/bin/$b || true; \
       done

# ---- apktool ----
RUN wget -q -O /usr/local/bin/apktool.jar \
    https://github.com/iBotPeaches/Apktool/releases/download/v2.9.3/apktool_2.9.3.jar

# ---- ew-cli (emulator.wtf) ----
RUN wget -q -O /usr/local/bin/ew-cli \
    https://maven.emulator.wtf/releases/ew-cli \
    && chmod +x /usr/local/bin/ew-cli

# ---- Python dependencies ----
# pillow  -> screenshot downscaling + coordinate-grid overlay (vision)
# websockets -> Chrome DevTools Protocol eval inside WebViews
RUN pip install --no-cache-dir \
    mcp starlette uvicorn httpx pillow websockets requests

# Optional: Play Store APK fetcher (non-fatal if unavailable)
RUN pip install --no-cache-dir apkeep || true

RUN mkdir -p /workspace
WORKDIR /workspace

COPY mcp_server.py /app/mcp_server.py

EXPOSE 8080
CMD ["python", "/app/mcp_server.py"]
