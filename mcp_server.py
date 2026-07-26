#!/usr/bin/env python3
"""Android RE MCP Server — Zerops (FastMCP version)
15 tools for APK reverse engineering and emulator.wtf control.
Uses FastMCP with SSE transport for Notion AI integration.
"""

import asyncio
import os
import subprocess
import shutil
from mcp.server.fastmcp import FastMCP
import httpx

mcp = FastMCP("Zerops Android RE")

WORK_DIR = os.environ.get("WORK_DIR", "/workspace")
APKTOOL_JAR = "/usr/local/bin/apktool.jar"
EW_CLI = "/usr/local/bin/ew-cli"
os.makedirs(WORK_DIR, exist_ok=True)


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


@mcp.tool()
async def run_command(command: str, cwd: str = "", timeout: int = 300) -> str:
    """Run a shell command. Returns stdout, stderr, exit code."""
    r = await run_cmd(command, cwd=cwd or None, timeout=timeout)
    return f"$ {command}\n\nSTDOUT:\n{r['stdout']}\n\nSTDERR:\n{r['stderr']}\n\nEXIT: {r['returncode']}"


@mcp.tool()
async def download_file(url: str, filename: str) -> str:
    """Download a file from URL to workspace."""
    fp = abs_path(filename)
    async with httpx.AsyncClient() as c:
        resp = await c.get(url, follow_redirects=True)
        with open(fp, "wb") as f:
            f.write(resp.content)
    return f"Downloaded {url} -> {fp} ({len(resp.content)} bytes)"


@mcp.tool()
async def decompile_apk(apk_path: str, output_dir: str) -> str:
    """Decompile APK using apktool."""
    apk, out = abs_path(apk_path), abs_path(output_dir)
    r = await run_cmd(["java", "-jar", APKTOOL_JAR, "d", "-f", apk, "-o", out], timeout=600)
    return f"Decompile {apk} -> {out}\n{r['stdout']}\n{r['stderr']}\nEXIT: {r['returncode']}"


@mcp.tool()
async def list_files(path: str, recursive: bool = False) -> str:
    """List files in a directory."""
    p = abs_path(path)
    depth = "10" if recursive else "1"
    r = await run_cmd(["find", p, "-maxdepth", depth, "-type", "f", "-o", "-type", "d"], timeout=30)
    return r["stdout"]


@mcp.tool()
async def read_file(path: str) -> str:
    """Read file contents."""
    p = abs_path(path)
    try:
        with open(p, "r", errors="replace") as f:
            return f.read()
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
async def write_file(path: str, content: str) -> str:
    """Write content to a file. Creates parent dirs."""
    p = abs_path(path)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(content)
    return f"Written {len(content)} bytes -> {p}"


@mcp.tool()
async def search_in_files(pattern: str, path: str, file_pattern: str = "") -> str:
    """Search for pattern in files using grep."""
    p = abs_path(path)
    cmd = f"grep -rn --include='{file_pattern}' '{pattern}' '{p}'" if file_pattern else f"grep -rn '{pattern}' '{p}'"
    r = await run_cmd(cmd, timeout=60)
    return r["stdout"] or "No matches"


@mcp.tool()
async def rebuild_apk(source_dir: str, output_apk: str) -> str:
    """Rebuild decompiled APK using apktool."""
    sd, oa = abs_path(source_dir), abs_path(output_apk)
    r = await run_cmd(["java", "-jar", APKTOOL_JAR, "b", "-f", sd, "-o", oa], timeout=600)
    return f"Rebuild {sd} -> {oa}\n{r['stdout']}\n{r['stderr']}\nEXIT: {r['returncode']}"


@mcp.tool()
async def sign_apk(apk_path: str) -> str:
    """Sign APK with debug keystore. Creates one if needed."""
    apk = abs_path(apk_path)
    ks = os.path.join(WORK_DIR, "debug.keystore")
    if not os.path.exists(ks):
        await run_cmd(["keytool", "-genkeypair", "-alias", "androiddebugkey", "-keypass", "android", "-keystore", ks, "-storepass", "android", "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000", "-dname", "CN=Android Debug,O=Android,C=US"])
    signer = shutil.which("apksigner")
    if signer:
        r = await run_cmd([signer, "sign", "--ks", ks, "--ks-key-alias", "androiddebugkey", "--ks-pass", "pass:android", "--key-pass", "pass:android", apk])
    else:
        r = await run_cmd(["jarsigner", "-keystore", ks, "-storepass", "android", "-keypass", "android", apk, "androiddebugkey"])
    return f"Sign {apk}\n{r['stdout']}\n{r['stderr']}\nEXIT: {r['returncode']}"


@mcp.tool()
async def start_emulator(api_level: int = 33, image: str = "") -> str:
    """Start emulator.wtf session with ADB."""
    cmd = [EW_CLI, "start-session", "--adb"]
    cmd += ["--image", image] if image else ["--api-level", str(api_level)]
    r = await run_cmd(cmd, timeout=120)
    return f"Start emulator\n{r['stdout']}\n{r['stderr']}\nEXIT: {r['returncode']}"


@mcp.tool()
async def stop_emulator() -> str:
    """Stop emulator.wtf session."""
    r = await run_cmd([EW_CLI, "stop-session"], timeout=60)
    return f"Stop emulator\n{r['stdout']}\n{r['stderr']}\nEXIT: {r['returncode']}"


@mcp.tool()
async def adb_command(args: str) -> str:
    """Run ADB command against emulator."""
    r = await run_cmd(["adb"] + args.split(), timeout=60)
    return f"adb {args}\n{r['stdout']}\n{r['stderr']}\nEXIT: {r['returncode']}"


@mcp.tool()
async def adb_install(apk_path: str) -> str:
    """Install APK via ADB."""
    apk = abs_path(apk_path)
    r = await run_cmd(["adb", "install", "-r", "-t", apk], timeout=120)
    return f"Install {apk}\n{r['stdout']}\n{r['stderr']}\nEXIT: {r['returncode']}"


@mcp.tool()
async def adb_screenshot(output_path: str = "") -> str:
    """Take screenshot via ADB."""
    out = abs_path(output_path) if output_path else os.path.join(WORK_DIR, "screenshot.png")
    await run_cmd(f"adb exec-out screencap -p > {out}", timeout=30)
    if os.path.exists(out):
        return f"Screenshot saved -> {out}"
    return "Screenshot failed"


@mcp.tool()
async def adb_logcat(filter: str = "", lines: int = 100) -> str:
    """Get logcat output from emulator."""
    cmd = ["adb", "logcat", "-d", "-t", str(lines)]
    if filter:
        cmd.append(filter)
    r = await run_cmd(cmd, timeout=30)
    return r["stdout"]


if __name__ == "__main__":
    mcp.run(transport="sse", host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
