#!/usr/bin/env python3
"""Android RE MCP Server — Zerops (v13 Streamable HTTP)
Adds StreamableHTTPServerTransport for POST / support (Notion requires this).
Keeps SSE at /sse as fallback.
"""

import asyncio
import os
import subprocess
import shutil
import traceback
import json
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import Tool, TextContent
import uvicorn
import httpx

# Try to import Streamable HTTP transport (available in newer MCP SDK versions)
try:
    from mcp.server.streamable_http import StreamableHTTPServerTransport
    HAS_STREAMABLE = True
except ImportError:
    HAS_STREAMABLE = False

server = Server("android-re-mcp")
sse = SseServerTransport("/messages/")

WORK_DIR = os.environ.get("WORK_DIR", "/workspace")
APKTOOL_JAR = "/usr/local/bin/apktool.jar"
EW_CLI = "/usr/local/bin/ew-cli"
os.makedirs(WORK_DIR, exist_ok=True)

# Streamable HTTP transport (initialized on first request)
_streamable_transport = None
_streamable_cm = None
_streamable_ready = False

async def init_streamable():
    global _streamable_transport, _streamable_cm, _streamable_ready
    if not _streamable_ready and HAS_STREAMABLE:
        _streamable_ready = True
        try:
            _streamable_transport = StreamableHTTPServerTransport(
                mcp_session_id=None,
                is_json_response_enabled=True
            )
            # Enter connect() context manager manually (keep alive for server lifetime)
            cm = _streamable_transport.connect()
            read, write = await cm.__aenter__()
            _streamable_cm = cm
            # Start server.run() in background task
            asyncio.create_task(
                server.run(read, write, server.create_initialization_options())
            )
            # Small delay to let server initialize
            await asyncio.sleep(0.05)
            print("Streamable HTTP transport initialized", flush=True)
        except Exception as e:
            print(f"Streamable HTTP init error: {e}", flush=True)
            traceback.print_exc()
            _streamable_ready = False


async def run_cmd(cmd, cwd=None, timeout=300):
    try:
        if isinstance(cmd, str):
            proc = await asyncio.create_subprocess_shell(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd or WORK_DIR)
        else:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd or WORK_DIR)
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return {"returncode": proc.returncode, "stdout": stdout.decode("utf-8", errors="replace"), "stderr": stderr.decode("utf-8", errors="replace")}
    except asyncio.TimeoutError:
        return {"returncode": -1, "stdout": "", "stderr": "Command timed out"}
    except Exception as e:
        return {"returncode": -1, "stdout": "", "stderr": str(e)}


def abs_path(path):
    return path if os.path.isabs(path) else os.path.join(WORK_DIR, path)


@server.list_tools()
async def list_tools():
    return [
        Tool(name="run_command", description="Run a shell command. Returns stdout, stderr, exit code.", inputSchema={"type":"object","properties":{"command":{"type":"string","description":"Shell command to execute"},"cwd":{"type":"string"},"timeout":{"type":"integer","default":300}},"required":["command"]}),
        Tool(name="download_file", description="Download a file from URL to workspace.", inputSchema={"type":"object","properties":{"url":{"type":"string"},"filename":{"type":"string"}},"required":["url","filename"]}),
        Tool(name="decompile_apk", description="Decompile APK using apktool.", inputSchema={"type":"object","properties":{"apk_path":{"type":"string"},"output_dir":{"type":"string"}},"required":["apk_path","output_dir"]}),
        Tool(name="list_files", description="List files in a directory.", inputSchema={"type":"object","properties":{"path":{"type":"string"},"recursive":{"type":"boolean","default":False}},"required":["path"]}),
        Tool(name="read_file", description="Read file contents.", inputSchema={"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}),
        Tool(name="write_file", description="Write content to a file. Creates parent dirs.", inputSchema={"type":"object","properties":{"path":{"type":"string"},"content":{"type":"string"}},"required":["path","content"]}),
        Tool(name="search_in_files", description="Search for pattern in files using grep.", inputSchema={"type":"object","properties":{"pattern":{"type":"string"},"path":{"type":"string"},"file_pattern":{"type":"string"}},"required":["pattern","path"]}),
        Tool(name="rebuild_apk", description="Rebuild decompiled APK using apktool.", inputSchema={"type":"object","properties":{"source_dir":{"type":"string"},"output_apk":{"type":"string"}},"required":["source_dir","output_apk"]}),
        Tool(name="sign_apk", description="Sign APK with debug keystore. Creates one if needed.", inputSchema={"type":"object","properties":{"apk_path":{"type":"string"}},"required":["apk_path"]}),
        Tool(name="start_emulator", description="Start emulator.wtf session with ADB.", inputSchema={"type":"object","properties":{"api_level":{"type":"integer","default":33},"image":{"type":"string"}}}),
        Tool(name="stop_emulator", description="Stop emulator.wtf session.", inputSchema={"type":"object","properties":{}}),
        Tool(name="adb_command", description="Run ADB command against emulator.", inputSchema={"type":"object","properties":{"args":{"type":"string"}},"required":["args"]}),
        Tool(name="adb_install", description="Install APK via ADB.", inputSchema={"type":"object","properties":{"apk_path":{"type":"string"}},"required":["apk_path"]}),
        Tool(name="adb_screenshot", description="Take screenshot via ADB.", inputSchema={"type":"object","properties":{"output_path":{"type":"string"}}}),
        Tool(name="adb_logcat", description="Get logcat output from emulator.", inputSchema={"type":"object","properties":{"filter":{"type":"string"},"lines":{"type":"integer","default":100}}}),
    ]


@server.call_tool()
async def call_tool(name, arguments):
    if name == "run_command":
        r = await run_cmd(arguments["command"], cwd=arguments.get("cwd"), timeout=arguments.get("timeout", 300))
        return [TextContent(type="text", text=f"$ {arguments['command']}\n\nSTDOUT:\n{r['stdout']}\n\nSTDERR:\n{r['stderr']}\n\nEXIT: {r['returncode']}")]
    elif name == "download_file":
        url, fn = arguments["url"], arguments["filename"]
        fp = abs_path(fn)
        async with httpx.AsyncClient() as c:
            resp = await c.get(url, follow_redirects=True)
            with open(fp, "wb") as f:
                f.write(resp.content)
        return [TextContent(type="text", text=f"Downloaded {url} -> {fp} ({len(resp.content)} bytes)")]
    elif name == "decompile_apk":
        apk, out = abs_path(arguments["apk_path"]), abs_path(arguments["output_dir"])
        r = await run_cmd(["java", "-jar", APKTOOL_JAR, "d", "-f", apk, "-o", out], timeout=600)
        return [TextContent(type="text", text=f"Decompile {apk} -> {out}\n{r['stdout']}\n{r['stderr']}\nEXIT: {r['returncode']}")]
    elif name == "list_files":
        p = abs_path(arguments["path"])
        depth = "10" if arguments.get("recursive") else "1"
        r = await run_cmd(["find", p, "-maxdepth", depth, "-type", "f", "-o", "-type", "d"], timeout=30)
        return [TextContent(type="text", text=r["stdout"])]
    elif name == "read_file":
        p = abs_path(arguments["path"])
        try:
            with open(p, "r", errors="replace") as f:
                return [TextContent(type="text", text=f.read())]
        except Exception as e:
            return [TextContent(type="text", text=f"Error: {e}")]
    elif name == "write_file":
        p = abs_path(arguments["path"])
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(arguments["content"])
        return [TextContent(type="text", text=f"Written {len(arguments['content'])} bytes -> {p}")]
    elif name == "search_in_files":
        pat, p = arguments["pattern"], abs_path(arguments["path"])
        fp = arguments.get("file_pattern", "")
        cmd = f"grep -rn --include='{fp}' '{pat}' '{p}'" if fp else f"grep -rn '{pat}' '{p}'"
        r = await run_cmd(cmd, timeout=60)
        return [TextContent(type="text", text=r["stdout"] or "No matches")]
    elif name == "rebuild_apk":
        sd, oa = abs_path(arguments["source_dir"]), abs_path(arguments["output_apk"])
        r = await run_cmd(["java", "-jar", APKTOOL_JAR, "b", "-f", sd, "-o", oa], timeout=600)
        return [TextContent(type="text", text=f"Rebuild {sd} -> {oa}\n{r['stdout']}\n{r['stderr']}\nEXIT: {r['returncode']}")]
    elif name == "sign_apk":
        apk = abs_path(arguments["apk_path"])
        ks = os.path.join(WORK_DIR, "debug.keystore")
        if not os.path.exists(ks):
            await run_cmd(["keytool", "-genkeypair", "-alias", "androiddebugkey", "-keypass", "android", "-keystore", ks, "-storepass", "android", "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000", "-dname", "CN=Android Debug,O=Android,C=US"])
        signer = shutil.which("apksigner")
        if signer:
            r = await run_cmd([signer, "sign", "--ks", ks, "--ks-key-alias", "androiddebugkey", "--ks-pass", "pass:android", "--key-pass", "pass:android", apk])
        else:
            r = await run_cmd(["jarsigner", "-keystore", ks, "-storepass", "android", "-keypass", "android", apk, "androiddebugkey"])
        return [TextContent(type="text", text=f"Sign {apk}\n{r['stdout']}\n{r['stderr']}\nEXIT: {r['returncode']}")]
    elif name == "start_emulator":
        api = arguments.get("api_level", 33)
        img = arguments.get("image", "")
        cmd = [EW_CLI, "start-session", "--adb"]
        cmd += ["--image", img] if img else ["--api-level", str(api)]
        r = await run_cmd(cmd, timeout=120)
        return [TextContent(type="text", text=f"Start emulator\n{r['stdout']}\n{r['stderr']}\nEXIT: {r['returncode']}")]
    elif name == "stop_emulator":
        r = await run_cmd([EW_CLI, "stop-session"], timeout=60)
        return [TextContent(type="text", text=f"Stop emulator\n{r['stdout']}\n{r['stderr']}\nEXIT: {r['returncode']}")]
    elif name == "adb_command":
        r = await run_cmd(["adb"] + arguments["args"].split(), timeout=60)
        return [TextContent(type="text", text=f"adb {arguments['args']}\n{r['stdout']}\n{r['stderr']}\nEXIT: {r['returncode']}")]
    elif name == "adb_install":
        apk = abs_path(arguments["apk_path"])
        r = await run_cmd(["adb", "install", "-r", "-t", apk], timeout=120)
        return [TextContent(type="text", text=f"Install {apk}\n{r['stdout']}\n{r['stderr']}\nEXIT: {r['returncode']}")]
    elif name == "adb_screenshot":
        out = arguments.get("output_path", os.path.join(WORK_DIR, "screenshot.png"))
        await run_cmd(f"adb exec-out screencap -p > {out}", timeout=30)
        if os.path.exists(out):
            return [TextContent(type="text", text=f"Screenshot saved -> {out}")]
        return [TextContent(type="text", text="Screenshot failed")]
    elif name == "adb_logcat":
        filt = arguments.get("filter", "")
        lines = arguments.get("lines", 100)
        cmd = ["adb", "logcat", "-d", "-t", str(lines)]
        if filt:
            cmd.append(filt)
        r = await run_cmd(cmd, timeout=30)
        return [TextContent(type="text", text=r["stdout"])]
    return [TextContent(type="text", text=f"Unknown tool: {name}")]


CORS_HEADERS = [
    [b"access-control-allow-origin", b"*"],
    [b"access-control-allow-methods", b"GET, HEAD, POST, DELETE, OPTIONS"],
    [b"access-control-allow-headers", b"*"],
]

SSE_HEADERS = [
    [b"content-type", b"text/event-stream"],
    [b"cache-control", b"no-cache"],
    [b"connection", b"keep-alive"],
    [b"x-accel-buffering", b"no"],
    [b"access-control-allow-origin", b"*"],
]

JSON_HEADERS = [[b"content-type", b"application/json"]] + CORS_HEADERS


def make_send_wrapper(send):
    async def send_wrapper(message):
        if message.get("type") == "http.response.start":
            headers = message.get("headers", [])
            existing = set()
            for h in headers:
                try:
                    existing.add(h[0].lower())
                except (IndexError, TypeError, KeyError):
                    pass
            extra = []
            if b"x-accel-buffering" not in existing:
                extra.append([b"x-accel-buffering", b"no"])
            if b"access-control-allow-origin" not in existing:
                extra.append([b"access-control-allow-origin", b"*"])
            if extra:
                message = {**message, "headers": list(headers) + extra}
        await send(message)
    return send_wrapper


async def handle_sse(scope, receive, send):
    """SSE handler for /sse path (fallback)."""
    wrapped_send = make_send_wrapper(send)
    try:
        print("SSE: GET connection started at /sse", flush=True)
        async with sse.connect_sse(scope, receive, wrapped_send) as (read, write):
            print("SSE: streams connected, starting server.run()", flush=True)
            await server.run(read, write, server.create_initialization_options())
            print("SSE: server.run() completed", flush=True)
    except Exception as e:
        err = traceback.format_exc()
        print(f"SSE Error: {err}", flush=True)
        try:
            body = json.dumps({"error": str(e), "traceback": err}).encode()
            await send({"type": "http.response.start", "status": 500, "headers": JSON_HEADERS})
            await send({"type": "http.response.body", "body": body})
        except:
            pass


class ASGIApp:
    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return
        path = scope["path"]
        method = scope["method"]

        # CORS preflight
        if method == "OPTIONS":
            await send({"type": "http.response.start", "status": 200, "headers": CORS_HEADERS})
            await send({"type": "http.response.body", "body": b""})
            return

        # HEAD / and /sse
        if path in ("/sse", "/") and method == "HEAD":
            await send({"type": "http.response.start", "status": 200, "headers": SSE_HEADERS})
            await send({"type": "http.response.body", "body": b""})
            return

        # Streamable HTTP at root / (POST, GET, DELETE) — Notion's preferred transport
        if path == "/" and method in ("POST", "GET", "DELETE") and HAS_STREAMABLE:
            await init_streamable()
            if _streamable_transport:
                try:
                    await _streamable_transport.handle_request(scope, receive, send)
                    return
                except Exception as e:
                    print(f"Streamable HTTP error: {e}", flush=True)
                    traceback.print_exc()
                    # Fall through to SSE if streamable fails

        # SSE at /sse (fallback) and / (if streamable not available)
        if path in ("/sse", "/") and method == "GET":
            await handle_sse(scope, receive, send)
            return

        # .well-known/mcp.json
        if path == "/.well-known/mcp.json" and method in ("GET", "HEAD"):
            body = json.dumps({
                "name": "android-re-mcp",
                "version": "1.0.0",
                "transports": {"sse": "/sse", "streamable_http": "/"}
            }).encode()
            await send({"type": "http.response.start", "status": 200, "headers": JSON_HEADERS})
            if method == "GET":
                await send({"type": "http.response.body", "body": body})
            else:
                await send({"type": "http.response.body", "body": b""})
            return

        # OAuth Protected Resource Metadata
        if path == "/.well-known/oauth-protected-resource/sse" and method in ("GET", "HEAD"):
            body = json.dumps({
                "resource": "https://docker-1be-8080.ny1.zerops.app/sse",
                "authorization_servers": [],
                "bearer_methods": ["header"],
                "scopes_supported": []
            }).encode()
            await send({"type": "http.response.start", "status": 200, "headers": JSON_HEADERS})
            if method == "GET":
                await send({"type": "http.response.body", "body": body})
            else:
                await send({"type": "http.response.body", "body": b""})
            return

        if path == "/.well-known/oauth-protected-resource" and method in ("GET", "HEAD"):
            body = json.dumps({
                "resource": "https://docker-1be-8080.ny1.zerops.app",
                "authorization_servers": [],
                "bearer_methods": ["header"],
                "scopes_supported": []
            }).encode()
            await send({"type": "http.response.start", "status": 200, "headers": JSON_HEADERS})
            if method == "GET":
                await send({"type": "http.response.body", "body": body})
            else:
                await send({"type": "http.response.body", "body": b""})
            return

        # OAuth Authorization Server — 404 (no OAuth server)
        if path == "/.well-known/oauth-authorization-server" and method in ("GET", "HEAD"):
            await send({"type": "http.response.start", "status": 404, "headers": JSON_HEADERS})
            await send({"type": "http.response.body", "body": b""})
            return

        # OpenID Configuration — 404
        if path == "/.well-known/openid-configuration" and method in ("GET", "HEAD"):
            await send({"type": "http.response.start", "status": 404, "headers": JSON_HEADERS})
            await send({"type": "http.response.body", "body": b"{\"error\":\"no_openid_support\"}"})
            return

        # Health endpoint
        if path == "/health" and method == "GET":
            body = json.dumps({"status": "ok", "tools": 15, "version": "v13-streamable-http", "streamable": HAS_STREAMABLE}).encode()
            await send({"type": "http.response.start", "status": 200, "headers": JSON_HEADERS})
            await send({"type": "http.response.body", "body": body})
            return

        # SSE Messages endpoint
        if path.startswith("/messages/") and method == "POST":
            await sse.handle_post_message(scope, receive, send)
            return

        # 404
        await send({"type": "http.response.start", "status": 404, "headers": [[b"content-type", b"text/plain"]]})
        await send({"type": "http.response.body", "body": b"Not Found"})


if __name__ == "__main__":
    uvicorn.run(ASGIApp(), host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
