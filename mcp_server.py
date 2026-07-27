#!/usr/bin/env python3
"""Android RE MCP Server - Zerops (v14.0 "powerhouse")

Upgrade over v13.1:
  * screen_view returns a REAL ImageContent (base64 PNG) so vision models can SEE the screen.
  * Full input control: ui_tap / ui_swipe / ui_text / ui_key / ui_find_tap.
  * Semantic screen reading: ui_dump (compact parsed uiautomator hierarchy).
  * WebView introspection via Chrome DevTools Protocol: devtools_targets / webview_eval.
  * Performance profiling: perf_gfxinfo (jank + frame percentiles), perf_meminfo.
  * Background jobs: job_start / job_status / job_logs  -> avoids MCP request timeouts.
  * Split-APK aware: adb_install_multiple, apk_sign_all, apk_info.
  * read_file supports offset/limit and binary (base64) mode.
"""

import asyncio
import os
import subprocess
import shutil
import traceback
import json
import base64
import io
import re
import time
import uuid
import xml.etree.ElementTree as ET

from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import Tool, TextContent, ImageContent
import uvicorn
import httpx

try:
    from PIL import Image, ImageDraw
    HAS_PIL = True
except Exception:
    HAS_PIL = False

try:
    import websockets
    HAS_WS = True
except Exception:
    HAS_WS = False

try:
    from mcp.server.streamable_http import StreamableHTTPServerTransport
    HAS_STREAMABLE = True
except ImportError:
    HAS_STREAMABLE = False

VERSION = "v14.0-powerhouse"

server = Server("android-re-mcp")
sse = SseServerTransport("/messages/")

WORK_DIR = os.environ.get("WORK_DIR", "/workspace")
APKTOOL_JAR = "/usr/local/bin/apktool.jar"
EW_CLI = "/usr/local/bin/ew-cli"
JOB_DIR = os.path.join(WORK_DIR, ".jobs")
os.makedirs(WORK_DIR, exist_ok=True)
os.makedirs(JOB_DIR, exist_ok=True)

# Extra search paths so android build-tools installed at runtime are found.
EXTRA_PATHS = [
    "/tmp/android-14/android-14", "/tmp/android-14",
    "/opt/android/build-tools", "/usr/local/bin",
]

_streamable_transport = None
_streamable_cm = None
_streamable_ready = False


async def init_streamable():
    global _streamable_transport, _streamable_cm, _streamable_ready
    if not _streamable_ready and HAS_STREAMABLE:
        _streamable_ready = True
        try:
            _streamable_transport = StreamableHTTPServerTransport(
                mcp_session_id=None, is_json_response_enabled=True)
            cm = _streamable_transport.connect()
            read, write = await cm.__aenter__()
            _streamable_cm = cm
            asyncio.create_task(server.run(read, write, server.create_initialization_options()))
            await asyncio.sleep(0.05)
            print("Streamable HTTP transport initialized", flush=True)
        except Exception as e:
            print(f"Streamable HTTP init error: {e}", flush=True)
            traceback.print_exc()
            _streamable_ready = False


# ----------------------------------------------------------------------------
# generic helpers
# ----------------------------------------------------------------------------

async def run_cmd(cmd, cwd=None, timeout=300):
    try:
        if isinstance(cmd, str):
            proc = await asyncio.create_subprocess_shell(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd or WORK_DIR)
        else:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd or WORK_DIR)
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return {"returncode": proc.returncode,
                "stdout": stdout.decode("utf-8", errors="replace"),
                "stderr": stderr.decode("utf-8", errors="replace")}
    except asyncio.TimeoutError:
        return {"returncode": -1, "stdout": "", "stderr": f"Command timed out after {timeout}s (tip: use job_start for long tasks)"}
    except Exception as e:
        return {"returncode": -1, "stdout": "", "stderr": str(e)}


def abs_path(path):
    return path if os.path.isabs(path) else os.path.join(WORK_DIR, path)


def which(name):
    p = shutil.which(name)
    if p:
        return p
    for d in EXTRA_PATHS:
        cand = os.path.join(d, name)
        if os.path.exists(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def get_adb_serial():
    try:
        result = subprocess.run(["adb", "devices"], capture_output=True, text=True, timeout=5)
        for line in result.stdout.strip().split("\n")[1:]:
            parts = line.strip().split("\t")
            if len(parts) >= 2 and parts[1].strip() == "device":
                return parts[0].strip()
    except Exception:
        pass
    return None


def adb_prefix(serial):
    s = serial or get_adb_serial()
    return (["adb", "-s", s], s) if s else (["adb"], None)


async def adb(args, serial=None, timeout=60):
    """args: list of adb arguments (already split)."""
    pre, s = adb_prefix(serial)
    return await run_cmd(pre + list(args), timeout=timeout), s


async def adb_shell_str(cmdstr, serial=None, timeout=60):
    """Run a shell command string on device without host-side word splitting issues."""
    pre, s = adb_prefix(serial)
    quoted = cmdstr.replace("'", "'\\''")
    full = " ".join(pre) + " shell '" + quoted + "'"
    return await run_cmd(full, timeout=timeout), s


def err(msg):
    return [TextContent(type="text", text=msg)]


def fmt(r, header=""):
    parts = []
    if header:
        parts.append(header)
    if r.get("stdout"):
        parts.append(r["stdout"].rstrip())
    if r.get("stderr"):
        parts.append("STDERR:\n" + r["stderr"].rstrip())
    parts.append(f"EXIT: {r['returncode']}")
    return [TextContent(type="text", text="\n".join(parts))]


# ----------------------------------------------------------------------------
# screenshot -> ImageContent
# ----------------------------------------------------------------------------

async def capture_png(serial=None, save_path=None):
    """Capture a screenshot, return (png_bytes, path_or_None, error_or_None)."""
    out = save_path or os.path.join(WORK_DIR, "_shot.png")
    pre, s = adb_prefix(serial)
    cmd = " ".join(pre) + f" exec-out screencap -p > '{out}'"
    r = await run_cmd(cmd, timeout=60)
    if not os.path.exists(out) or os.path.getsize(out) == 0:
        return None, None, f"screencap failed (serial={s}) {r.get('stderr','')}"
    with open(out, "rb") as f:
        return f.read(), out, None


def process_image(png_bytes, max_width=720, grid=False, quality=None):
    """Downscale (token control) and optionally overlay a coordinate grid.
    Returns (base64_str, mime, info_text)."""
    if not HAS_PIL:
        return base64.b64encode(png_bytes).decode(), "image/png", "raw (PIL unavailable)"
    im = Image.open(io.BytesIO(png_bytes))
    ow, oh = im.size
    if im.mode not in ("RGB", "RGBA"):
        im = im.convert("RGB")
    scale = 1.0
    if max_width and ow > max_width:
        scale = max_width / float(ow)
        im = im.resize((int(ow * scale), int(oh * scale)), Image.LANCZOS)
    if grid:
        im = im.convert("RGB")
        d = ImageDraw.Draw(im)
        w, h = im.size
        step = 100  # device px between grid lines
        for dx in range(0, ow, step * 2):
            x = int(dx * scale)
            d.line([(x, 0), (x, h)], fill=(255, 0, 0), width=1)
            d.text((x + 2, 2), str(dx), fill=(255, 0, 0))
        for dy in range(0, oh, step * 2):
            y = int(dy * scale)
            d.line([(0, y), (w, y)], fill=(0, 0, 255), width=1)
            d.text((2, y + 2), str(dy), fill=(0, 0, 255))
    buf = io.BytesIO()
    if quality:
        im.convert("RGB").save(buf, format="JPEG", quality=int(quality))
        mime = "image/jpeg"
    else:
        im.save(buf, format="PNG", optimize=True)
        mime = "image/png"
    data = buf.getvalue()
    info = f"device {ow}x{oh} -> sent {im.size[0]}x{im.size[1]} ({len(data)} bytes{', grid overlay: red=X blue=Y in DEVICE px' if grid else ''})"
    return base64.b64encode(data).decode(), mime, info


async def screen_content(serial=None, max_width=720, grid=False, quality=None,
                         save_path=None, note=""):
    png, path, e = await capture_png(serial, save_path)
    if e:
        return err(e)
    b64, mime, info = process_image(png, max_width, grid, quality)
    head = (note + "\n" if note else "") + info + (f"\nsaved: {path}" if path else "")
    return [TextContent(type="text", text=head),
            ImageContent(type="image", data=b64, mimeType=mime)]


# ----------------------------------------------------------------------------
# uiautomator hierarchy
# ----------------------------------------------------------------------------

BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


def parse_bounds(b):
    m = BOUNDS_RE.search(b or "")
    if not m:
        return None
    x1, y1, x2, y2 = (int(g) for g in m.groups())
    return x1, y1, x2, y2


def center(b):
    p = parse_bounds(b)
    if not p:
        return None
    x1, y1, x2, y2 = p
    return (x1 + x2) // 2, (y1 + y2) // 2


async def fetch_hierarchy(serial=None):
    r, s = await adb(["shell", "uiautomator", "dump", "/sdcard/_ui.xml"], serial, timeout=60)
    if r["returncode"] != 0:
        return None, f"dump failed: {r['stderr'] or r['stdout']}"
    r2, _ = await adb(["shell", "cat", "/sdcard/_ui.xml"], serial, timeout=60)
    xml = r2["stdout"].strip()
    i = xml.find("<hierarchy")
    if i < 0:
        return None, "no hierarchy in dump output"
    return xml[i:], None


def walk_nodes(xml):
    try:
        root = ET.fromstring(xml)
    except Exception as e:
        return [], str(e)
    out = []

    def rec(node, depth):
        a = node.attrib
        out.append({
            "depth": depth,
            "class": a.get("class", ""),
            "id": a.get("resource-id", ""),
            "text": a.get("text", ""),
            "desc": a.get("content-desc", ""),
            "clickable": a.get("clickable") == "true",
            "scrollable": a.get("scrollable") == "true",
            "enabled": a.get("enabled") == "true",
            "focused": a.get("focused") == "true",
            "bounds": a.get("bounds", ""),
            "pkg": a.get("package", ""),
        })
        for ch in list(node):
            rec(ch, depth + 1)

    for ch in list(root):
        rec(ch, 0)
    return out, None


def render_nodes(nodes, only_interesting=True):
    lines = []
    for n in nodes:
        has_info = n["text"] or n["desc"] or n["clickable"] or n["scrollable"] or n["id"]
        if only_interesting and not has_info:
            continue
        c = center(n["bounds"])
        flags = "".join(["C" if n["clickable"] else "-",
                         "S" if n["scrollable"] else "-",
                         "F" if n["focused"] else "-"])
        short = n["class"].split(".")[-1]
        bits = [f"{'  ' * min(n['depth'], 8)}{short}", f"[{flags}]"]
        if n["id"]:
            bits.append("id=" + n["id"].split("/")[-1])
        if n["text"]:
            bits.append("text=" + json.dumps(n["text"][:80]))
        if n["desc"]:
            bits.append("desc=" + json.dumps(n["desc"][:80]))
        if c:
            bits.append(f"tap=({c[0]},{c[1]})")
        lines.append(" ".join(bits))
    return lines


# ----------------------------------------------------------------------------
# background jobs (avoids MCP request timeouts on long operations)
# ----------------------------------------------------------------------------

def job_paths(jid):
    return (os.path.join(JOB_DIR, jid + ".log"),
            os.path.join(JOB_DIR, jid + ".pid"),
            os.path.join(JOB_DIR, jid + ".meta"))


async def start_job(command, cwd=None, label=""):
    jid = time.strftime("%H%M%S") + "-" + uuid.uuid4().hex[:4]
    log, pid, meta = job_paths(jid)
    wrapper = (
        f"nohup sh -c {json.dumps(command)} > {json.dumps(log)} 2>&1 &\n"
        f"echo $! > {json.dumps(pid)}\n"
        f"cat {json.dumps(pid)}\n"
    )
    r = await run_cmd(wrapper, cwd=cwd, timeout=30)
    with open(meta, "w") as f:
        json.dump({"id": jid, "command": command, "cwd": cwd or WORK_DIR,
                   "label": label, "started": time.time()}, f)
    return jid, r


def job_alive(jid):
    _, pidf, _ = job_paths(jid)
    try:
        with open(pidf) as f:
            pid = int(f.read().strip())
    except Exception:
        return None, False
    return pid, os.path.exists(f"/proc/{pid}")


def job_report(jid, tail=60):
    log, _, meta = job_paths(jid)
    if not os.path.exists(meta):
        return f"No such job: {jid}"
    info = json.load(open(meta))
    pid, alive = job_alive(jid)
    dur = int(time.time() - info["started"])
    body = ""
    if os.path.exists(log):
        with open(log, errors="replace") as f:
            lines = f.read().splitlines()
        body = "\n".join(lines[-tail:])
        total = len(lines)
    else:
        total = 0
    return (f"job {jid} [{'RUNNING' if alive else 'FINISHED'}] pid={pid} elapsed={dur}s "
            f"lines={total}\ncmd: {info['command']}\n--- last {tail} lines ---\n{body}")


# ----------------------------------------------------------------------------
# Chrome DevTools Protocol (WebView introspection)
# ----------------------------------------------------------------------------

async def cdp_setup(serial=None, local_port=9222, socket_name=None):
    """Forward a device devtools unix socket to a host TCP port.
    Returns (socket_name, error)."""
    pre, s = adb_prefix(serial)
    if not socket_name:
        r, _ = await adb(["shell", "cat", "/proc/net/unix"], serial, timeout=30)
        found = re.findall(r"@(webview_devtools_remote_\d+|chrome_devtools_remote_?\d*)",
                           r["stdout"])
        if not found:
            return None, ("No devtools socket on device. The app's WebView must have "
                          "WebView.setWebContentsDebuggingEnabled(true) (debuggable build), "
                          "or the app is not running.")
        socket_name = sorted(set(found))[0]
    await run_cmd(" ".join(pre) + f" forward --remove tcp:{local_port}", timeout=20)
    rf, _ = await adb(["forward", f"tcp:{local_port}", f"localabstract:{socket_name}"],
                      serial, timeout=30)
    if rf["returncode"] != 0:
        return None, f"adb forward failed: {rf['stderr'] or rf['stdout']}"
    return socket_name, None


async def cdp_list(local_port=9222):
    async with httpx.AsyncClient() as c:
        resp = await c.get(f"http://127.0.0.1:{local_port}/json/list", timeout=20)
        return resp.json()


async def cdp_eval(ws_url, expression, timeout=30):
    if not HAS_WS:
        raise RuntimeError("websockets library not installed")
    async with websockets.connect(ws_url, max_size=32 * 1024 * 1024,
                                  open_timeout=timeout) as ws:
        await ws.send(json.dumps({
            "id": 1, "method": "Runtime.evaluate",
            "params": {"expression": expression, "returnByValue": True,
                       "awaitPromise": True, "userGesture": True,
                       "timeout": int(timeout * 1000)}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = await asyncio.wait_for(ws.recv(), timeout=max(1, deadline - time.time()))
            msg = json.loads(raw)
            if msg.get("id") == 1:
                return msg
        raise TimeoutError("CDP evaluate timed out")


# ----------------------------------------------------------------------------
# performance parsing
# ----------------------------------------------------------------------------

GFX_KEYS = [
    "Total frames rendered", "Janky frames", "50th percentile", "90th percentile",
    "95th percentile", "99th percentile", "Number Missed Vsync",
    "Number High input latency", "Number Slow UI thread",
    "Number Slow bitmap uploads", "Number Slow issue draw commands",
    "Number Frame deadline missed",
]


def parse_gfxinfo(text):
    out = []
    for line in text.splitlines():
        ls = line.strip()
        for k in GFX_KEYS:
            if ls.startswith(k):
                out.append(ls)
                break
    frames = []
    for line in text.splitlines():
        if re.match(r"^0,\d+", line.strip()):
            frames.append(line.strip())
    summary = "\n".join(out) if out else "(no gfxinfo counters found)"
    if frames:
        summary += f"\n\nframestats rows captured: {len(frames)}"
    return summary


def parse_meminfo(text):
    keep = ("TOTAL", "Native Heap", "Dalvik Heap", "Graphics", "GL mtrack",
            "EGL mtrack", "Private Dirty", "TOTAL PSS", "Views:", "ViewRootImpl",
            "AppContexts", "Activities", "WebViews")
    lines = [l.rstrip() for l in text.splitlines()
             if any(k in l for k in keep)]
    return "\n".join(lines) if lines else text[:2000]


# ----------------------------------------------------------------------------
# tool declarations
# ----------------------------------------------------------------------------

S = lambda **p: {"type": "object", "properties": p}
STR = {"type": "string"}
INT = {"type": "integer"}
BOOL = {"type": "boolean"}
SERIAL = {"type": "string", "description": "Optional device serial. Auto-detected if omitted."}


@server.list_tools()
async def list_tools():
    return [
        # ---------------- shell / files ----------------
        Tool(name="run_command", description="Run a shell command. Returns stdout, stderr, exit code. For anything that may exceed ~4 min, use job_start instead.",
             inputSchema={"type": "object", "properties": {"command": STR, "cwd": STR, "timeout": {"type": "integer", "default": 300}}, "required": ["command"]}),
        Tool(name="job_start", description="Run a long shell command in the BACKGROUND and return a job id immediately. Never times out. Poll with job_status.",
             inputSchema={"type": "object", "properties": {"command": STR, "cwd": STR, "label": STR}, "required": ["command"]}),
        Tool(name="job_status", description="Check a background job: running/finished, elapsed time, and the tail of its output.",
             inputSchema={"type": "object", "properties": {"job_id": STR, "tail": {"type": "integer", "default": 60}}, "required": ["job_id"]}),
        Tool(name="job_logs", description="Read a background job's full log, optionally from a line offset.",
             inputSchema={"type": "object", "properties": {"job_id": STR, "offset": {"type": "integer", "default": 0}, "limit": {"type": "integer", "default": 400}}, "required": ["job_id"]}),
        Tool(name="job_list", description="List all background jobs and their state (running or finished).",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="download_file", description="Download a file from URL to workspace.",
             inputSchema={"type": "object", "properties": {"url": STR, "filename": STR}, "required": ["url", "filename"]}),
        Tool(name="http_request", description="Make an arbitrary HTTP request (GET/POST/...) and return status, headers and body. Useful for APIs and token dispensers.",
             inputSchema={"type": "object", "properties": {"url": STR, "method": {"type": "string", "default": "GET"}, "headers": {"type": "object"}, "body": STR, "max_bytes": {"type": "integer", "default": 20000}}, "required": ["url"]}),
        Tool(name="list_files", description="List files in a directory (with sizes).",
             inputSchema={"type": "object", "properties": {"path": STR, "recursive": {"type": "boolean", "default": False}}, "required": ["path"]}),
        Tool(name="read_file", description="Read file contents. Supports line offset/limit for huge files, and binary mode (returns base64).",
             inputSchema={"type": "object", "properties": {"path": STR, "offset": {"type": "integer", "default": 0}, "limit": INT, "binary": {"type": "boolean", "default": False}}, "required": ["path"]}),
        Tool(name="write_file", description="Write content to a file. Creates parent dirs. Set append=true to append.",
             inputSchema={"type": "object", "properties": {"path": STR, "content": STR, "append": {"type": "boolean", "default": False}}, "required": ["path", "content"]}),
        Tool(name="search_in_files", description="Search for a pattern in files using grep -rn. Supports max_results and context lines.",
             inputSchema={"type": "object", "properties": {"pattern": STR, "path": STR, "file_pattern": STR, "max_results": {"type": "integer", "default": 200}, "context": {"type": "integer", "default": 0}, "fixed": {"type": "boolean", "default": False}}, "required": ["pattern", "path"]}),

        # ---------------- APK tooling ----------------
        Tool(name="decompile_apk", description="Decompile APK using apktool (runs in background if slow; returns job id when async=true).",
             inputSchema={"type": "object", "properties": {"apk_path": STR, "output_dir": STR, "async_job": {"type": "boolean", "default": False}, "no_res": {"type": "boolean", "default": False}, "no_src": {"type": "boolean", "default": False}}, "required": ["apk_path", "output_dir"]}),
        Tool(name="rebuild_apk", description="Rebuild decompiled APK using apktool.",
             inputSchema={"type": "object", "properties": {"source_dir": STR, "output_apk": STR, "async_job": {"type": "boolean", "default": False}}, "required": ["source_dir", "output_apk"]}),
        Tool(name="sign_apk", description="Zipalign + sign an APK with a keystore (auto-creates one). Returns verification output.",
             inputSchema={"type": "object", "properties": {"apk_path": STR, "keystore": STR, "align": {"type": "boolean", "default": True}}, "required": ["apk_path"]}),
        Tool(name="apk_sign_all", description="Zipalign + sign MANY APKs (a split set) with ONE shared keystore, then verify all certificate digests match. Essential for installable split bundles.",
             inputSchema={"type": "object", "properties": {"apk_paths": {"type": "array", "items": STR}, "output_dir": STR, "keystore": STR}, "required": ["apk_paths", "output_dir"]}),
        Tool(name="apk_info", description="Inspect an APK: package, version, sdk levels, split name, required split types, permissions, native ABIs, entry count, signature.",
             inputSchema={"type": "object", "properties": {"apk_path": STR, "verbose": {"type": "boolean", "default": False}}, "required": ["apk_path"]}),
        Tool(name="apk_replace_entry", description="Replace one file INSIDE an apk/zip without a full decompile+rebuild (uses jar/zip update). Preserves other entries. Ideal for patching bundled JS/assets.",
             inputSchema={"type": "object", "properties": {"apk_path": STR, "entry": {"type": "string", "description": "Path of the entry inside the APK, e.g. assets/bundle/x.js"}, "source_file": {"type": "string", "description": "Local file whose contents replace the entry."}}, "required": ["apk_path", "entry", "source_file"]}),

        # ---------------- emulator lifecycle ----------------
        Tool(name="start_emulator", description="Start an emulator.wtf session with ADB. Runs in background. model=Pixel7, version=API level 23-34, max_time like '30m'. Set wait_boot=true to block until sys.boot_completed.",
             inputSchema={"type": "object", "properties": {"model": {"type": "string", "default": "Pixel7"}, "version": {"type": "integer", "default": 33}, "max_time": {"type": "string", "default": "1h"}, "wait_boot": {"type": "boolean", "default": True}}}),
        Tool(name="stop_emulator", description="Stop the emulator.wtf session.", inputSchema={"type": "object", "properties": {}}),
        Tool(name="emulator_status", description="Report emulator health: adb devices, boot state, ABI list, screen size/density, and the session log tail.",
             inputSchema={"type": "object", "properties": {}}),

        # ---------------- adb basics ----------------
        Tool(name="adb_command", description="Run an ADB command. Auto-detects serial. Use shell_string for a device-side command that needs quoting/pipes.",
             inputSchema={"type": "object", "properties": {"args": STR, "shell_string": {"type": "string", "description": "Device shell command run verbatim (supports pipes, quotes, &&)."}, "serial": SERIAL, "timeout": {"type": "integer", "default": 60}}}),
        Tool(name="adb_install", description="Install a single APK via ADB.",
             inputSchema={"type": "object", "properties": {"apk_path": STR, "serial": SERIAL, "downgrade": {"type": "boolean", "default": False}}, "required": ["apk_path"]}),
        Tool(name="adb_install_multiple", description="Install a SPLIT APK SET together (base + config.* splits) using install-multiple. Required for App Bundle apps such as Notion.",
             inputSchema={"type": "object", "properties": {"apk_paths": {"type": "array", "items": STR}, "serial": SERIAL, "reinstall": {"type": "boolean", "default": True}}, "required": ["apk_paths"]}),
        Tool(name="adb_logcat", description="Get logcat output. Supports grep filtering and clearing first.",
             inputSchema={"type": "object", "properties": {"filter": STR, "grep": STR, "lines": {"type": "integer", "default": 100}, "clear_first": {"type": "boolean", "default": False}, "serial": SERIAL}}),
        Tool(name="adb_screenshot", description="Save a screenshot to a file path (text result only). Prefer screen_view if you want to actually SEE the screen.",
             inputSchema={"type": "object", "properties": {"output_path": STR, "serial": SERIAL}}),

        # ---------------- VISION + CONTROL ----------------
        Tool(name="screen_view", description="Capture the device screen and RETURN IT AS AN IMAGE so a vision model can see it directly. Set grid=true to overlay device-pixel coordinates for accurate tapping.",
             inputSchema={"type": "object", "properties": {"max_width": {"type": "integer", "default": 720, "description": "Downscale width to control token cost. Use 1080 for full detail."}, "grid": {"type": "boolean", "default": False}, "jpeg_quality": {"type": "integer", "description": "If set, send JPEG at this quality instead of PNG (smaller)."}, "save_path": STR, "serial": SERIAL}}),
        Tool(name="ui_dump", description="Dump the on-screen view hierarchy as compact text: class, resource-id, text, content-desc, flags and a ready-to-use tap coordinate. NOTE: content inside a WebView is invisible here - use screen_view or webview_eval for WebView apps.",
             inputSchema={"type": "object", "properties": {"all_nodes": {"type": "boolean", "default": False}, "contains": {"type": "string", "description": "Only show nodes whose text/desc/id contains this string."}, "serial": SERIAL}}),
        Tool(name="ui_tap", description="Tap at device coordinates. Set view_after=true to get a screenshot image of the result.",
             inputSchema={"type": "object", "properties": {"x": INT, "y": INT, "view_after": {"type": "boolean", "default": False}, "delay_ms": {"type": "integer", "default": 700}, "serial": SERIAL}, "required": ["x", "y"]}),
        Tool(name="ui_find_tap", description="Find an element by text / content-desc / resource-id and tap its center. Returns what was matched.",
             inputSchema={"type": "object", "properties": {"query": STR, "index": {"type": "integer", "default": 0}, "view_after": {"type": "boolean", "default": False}, "serial": SERIAL}, "required": ["query"]}),
        Tool(name="ui_swipe", description="Swipe/scroll from one point to another. duration_ms controls fling speed (use 800+ for a controlled scroll, 100 for a fast fling).",
             inputSchema={"type": "object", "properties": {"x1": INT, "y1": INT, "x2": INT, "y2": INT, "duration_ms": {"type": "integer", "default": 400}, "view_after": {"type": "boolean", "default": False}, "serial": SERIAL}, "required": ["x1", "y1", "x2", "y2"]}),
        Tool(name="ui_text", description="Type text into the focused field (handles spaces and special characters safely).",
             inputSchema={"type": "object", "properties": {"text": STR, "view_after": {"type": "boolean", "default": False}, "serial": SERIAL}, "required": ["text"]}),
        Tool(name="ui_key", description="Send a key event by name (BACK, HOME, ENTER, TAB, APP_SWITCH, DEL, MENU, POWER, VOLUME_UP...) or numeric keycode.",
             inputSchema={"type": "object", "properties": {"key": STR, "view_after": {"type": "boolean", "default": False}, "serial": SERIAL}, "required": ["key"]}),
        Tool(name="app_control", description="Launch, force-stop, clear, or query an app package. action = launch|stop|clear|info|current.",
             inputSchema={"type": "object", "properties": {"action": STR, "package": STR, "activity": STR, "serial": SERIAL}, "required": ["action"]}),

        # ---------------- WebView / CDP ----------------
        Tool(name="devtools_targets", description="List Chrome DevTools Protocol targets (pages/webviews) exposed by the device, forwarding the devtools socket automatically. Works only if the WebView is debuggable.",
             inputSchema={"type": "object", "properties": {"port": {"type": "integer", "default": 9222}, "socket": STR, "serial": SERIAL}}),
        Tool(name="webview_eval", description="Evaluate JavaScript INSIDE the running app's WebView via DevTools Protocol and return the result. Lets you inspect the live DOM, measure render performance, read app state, or flip runtime flags. Requires a debuggable WebView.",
             inputSchema={"type": "object", "properties": {"expression": STR, "target_index": {"type": "integer", "default": 0}, "port": {"type": "integer", "default": 9222}, "timeout": {"type": "integer", "default": 30}, "serial": SERIAL}, "required": ["expression"]}),

        # ---------------- performance ----------------
        Tool(name="perf_gfxinfo", description="Measure rendering performance / LAG for a package: total frames, janky frame percentage, and 50/90/95/99th percentile frame times. Set reset=true to zero counters before an interaction, then call again after.",
             inputSchema={"type": "object", "properties": {"package": STR, "reset": {"type": "boolean", "default": False}, "framestats": {"type": "boolean", "default": False}, "serial": SERIAL}, "required": ["package"]}),
        Tool(name="perf_meminfo", description="Memory profile for a package: total PSS, native/dalvik heap, graphics memory, view and WebView counts (useful for leak hunting).",
             inputSchema={"type": "object", "properties": {"package": STR, "serial": SERIAL}, "required": ["package"]}),
    ]


# ----------------------------------------------------------------------------
# tool dispatch
# ----------------------------------------------------------------------------

KEY_ALIASES = {"back": "KEYCODE_BACK", "home": "KEYCODE_HOME", "enter": "KEYCODE_ENTER",
               "tab": "KEYCODE_TAB", "del": "KEYCODE_DEL", "delete": "KEYCODE_DEL",
               "menu": "KEYCODE_MENU", "search": "KEYCODE_SEARCH",
               "appswitch": "KEYCODE_APP_SWITCH", "recents": "KEYCODE_APP_SWITCH",
               "power": "KEYCODE_POWER", "escape": "KEYCODE_ESCAPE"}


def norm_key(k):
    k = str(k).strip()
    if k.isdigit():
        return k
    low = k.lower().replace("_", "").replace("keycode", "")
    if low in KEY_ALIASES:
        return KEY_ALIASES[low]
    return k if k.upper().startswith("KEYCODE_") else "KEYCODE_" + k.upper()


@server.call_tool()
async def call_tool(name, arguments):
    a = arguments or {}
    serial = a.get("serial")
    try:
        # ================= shell / jobs / files =================
        if name == "run_command":
            r = await run_cmd(a["command"], cwd=a.get("cwd"), timeout=a.get("timeout", 300))
            return fmt(r, f"$ {a['command']}")

        if name == "job_start":
            jid, r = await start_job(a["command"], a.get("cwd"), a.get("label", ""))
            return err(f"Started background job {jid}\ncmd: {a['command']}\n"
                       f"pid: {r['stdout'].strip()}\nPoll with job_status(job_id='{jid}').")

        if name == "job_status":
            return err(job_report(a["job_id"], a.get("tail", 60)))

        if name == "job_logs":
            log, _, meta = job_paths(a["job_id"])
            if not os.path.exists(log):
                return err(f"no log for job {a['job_id']}")
            with open(log, errors="replace") as f:
                lines = f.read().splitlines()
            off = a.get("offset", 0)
            lim = a.get("limit", 400)
            chunk = lines[off:off + lim]
            return err(f"job {a['job_id']} lines {off}-{off + len(chunk)} of {len(lines)}\n"
                       + "\n".join(chunk))

        if name == "job_list":
            rows = []
            for f in sorted(os.listdir(JOB_DIR)):
                if not f.endswith(".meta"):
                    continue
                jid = f[:-5]
                info = json.load(open(os.path.join(JOB_DIR, f)))
                pid, alive = job_alive(jid)
                rows.append(f"{jid} [{'RUNNING' if alive else 'done'}] "
                            f"{int(time.time() - info['started'])}s :: {info['command'][:90]}")
            return err("\n".join(rows) or "no jobs")

        if name == "download_file":
            fp = abs_path(a["filename"])
            os.makedirs(os.path.dirname(fp) or WORK_DIR, exist_ok=True)
            async with httpx.AsyncClient(follow_redirects=True, timeout=600) as c:
                resp = await c.get(a["url"])
                with open(fp, "wb") as f:
                    f.write(resp.content)
            return err(f"Downloaded {a['url']} -> {fp} ({len(resp.content)} bytes, HTTP {resp.status_code})")

        if name == "http_request":
            mb = a.get("max_bytes", 20000)
            async with httpx.AsyncClient(follow_redirects=True, timeout=120) as c:
                resp = await c.request(a.get("method", "GET"), a["url"],
                                       headers=a.get("headers") or None,
                                       content=a.get("body"))
            hdrs = "\n".join(f"{k}: {v}" for k, v in resp.headers.items())
            body = resp.text[:mb]
            return err(f"HTTP {resp.status_code} {resp.reason_phrase}\n{hdrs}\n\n{body}")

        if name == "list_files":
            p = abs_path(a["path"])
            depth = "10" if a.get("recursive") else "1"
            r = await run_cmd(f"find {json.dumps(p)} -maxdepth {depth} -printf '%y %10s %p\\n' 2>/dev/null | head -500", timeout=60)
            return err(r["stdout"] or r["stderr"] or "(empty)")

        if name == "read_file":
            p = abs_path(a["path"])
            if not os.path.exists(p):
                return err(f"Error: no such file {p}")
            if a.get("binary"):
                with open(p, "rb") as f:
                    raw = f.read()
                return err(f"{p} ({len(raw)} bytes) base64:\n"
                           + base64.b64encode(raw).decode())
            with open(p, "r", errors="replace") as f:
                text = f.read()
            off, lim = a.get("offset", 0), a.get("limit")
            if off or lim:
                lines = text.splitlines()
                sel = lines[off: off + lim] if lim else lines[off:]
                return err(f"[lines {off}-{off + len(sel)} of {len(lines)}]\n" + "\n".join(sel))
            return err(text)

        if name == "write_file":
            p = abs_path(a["path"])
            os.makedirs(os.path.dirname(p) or WORK_DIR, exist_ok=True)
            with open(p, "a" if a.get("append") else "w") as f:
                f.write(a["content"])
            return err(f"{'Appended' if a.get('append') else 'Written'} "
                       f"{len(a['content'])} bytes -> {p}")

        if name == "search_in_files":
            p = abs_path(a["path"])
            flags = "-rn" + ("F" if a.get("fixed") else "")
            inc = f"--include={json.dumps(a['file_pattern'])} " if a.get("file_pattern") else ""
            ctx = f"-C {a['context']} " if a.get("context") else ""
            mx = a.get("max_results", 200)
            cmd = f"grep {flags} {ctx}{inc}-e {json.dumps(a['pattern'])} {json.dumps(p)} | head -{mx}"
            r = await run_cmd(cmd, timeout=180)
            return err(r["stdout"] or "No matches")

        # ================= APK tooling =================
        if name == "decompile_apk":
            apk, out = abs_path(a["apk_path"]), abs_path(a["output_dir"])
            extra = (["-r"] if a.get("no_res") else []) + (["-s"] if a.get("no_src") else [])
            cmd = ["java", "-jar", APKTOOL_JAR, "d", "-f"] + extra + [apk, "-o", out]
            if a.get("async_job"):
                jid, _ = await start_job(" ".join(json.dumps(x) for x in cmd), label="decompile")
                return err(f"Decompiling in background as job {jid} -> {out}")
            r = await run_cmd(cmd, timeout=900)
            return fmt(r, f"Decompile {apk} -> {out}")

        if name == "rebuild_apk":
            sd, oa = abs_path(a["source_dir"]), abs_path(a["output_apk"])
            cmd = ["java", "-jar", APKTOOL_JAR, "b", "-f", sd, "-o", oa]
            if a.get("async_job"):
                jid, _ = await start_job(" ".join(json.dumps(x) for x in cmd), label="rebuild")
                return err(f"Rebuilding in background as job {jid} -> {oa}")
            r = await run_cmd(cmd, timeout=900)
            return fmt(r, f"Rebuild {sd} -> {oa}")

        if name in ("sign_apk", "apk_sign_all"):
            ks = abs_path(a.get("keystore") or os.path.join(WORK_DIR, "debug.keystore"))
            alias = "androiddebugkey"
            if not os.path.exists(ks):
                kt = await run_cmd(["keytool", "-genkeypair", "-alias", alias,
                                    "-keypass", "android", "-keystore", ks,
                                    "-storepass", "android", "-keyalg", "RSA",
                                    "-keysize", "2048", "-validity", "10000",
                                    "-dname", "CN=Android Debug,O=Android,C=US"], timeout=120)
                if kt["returncode"] != 0:
                    return fmt(kt, "keystore creation failed")
            else:
                lst = await run_cmd(["keytool", "-list", "-keystore", ks,
                                     "-storepass", "android"], timeout=60)
                m = re.search(r"^(\S+), .*PrivateKeyEntry", lst["stdout"], re.M)
                if m:
                    alias = m.group(1).rstrip(",")
            signer = which("apksigner")
            aligner = which("zipalign")
            if not signer:
                return err("apksigner not found. Install Android build-tools "
                           "(e.g. dl.google.com/android/repository/build-tools_r34-linux.zip) "
                           "and place it on PATH or /tmp/android-14.")
            targets = [abs_path(x) for x in (a["apk_paths"] if name == "apk_sign_all"
                                             else [a["apk_path"]])]
            outdir = abs_path(a["output_dir"]) if name == "apk_sign_all" else None
            if outdir:
                os.makedirs(outdir, exist_ok=True)
            report, digests = [], {}
            for src in targets:
                dst = os.path.join(outdir, os.path.basename(src)) if outdir else src
                work = dst
                if a.get("align", True) and aligner:
                    tmp = dst + ".aligned"
                    ra = await run_cmd([aligner, "-p", "-f", "4", src, tmp], timeout=300)
                    if ra["returncode"] != 0:
                        report.append(f"{os.path.basename(src)}: zipalign FAILED {ra['stderr'][:200]}")
                        continue
                    os.replace(tmp, work)
                elif src != dst:
                    shutil.copy2(src, dst)
                rs = await run_cmd([signer, "sign", "--ks", ks, "--ks-key-alias", alias,
                                    "--ks-pass", "pass:android", "--key-pass", "pass:android",
                                    work], timeout=600)
                if rs["returncode"] != 0:
                    report.append(f"{os.path.basename(work)}: SIGN FAILED {rs['stderr'][:300]}")
                    continue
                rv = await run_cmd([signer, "verify", "--print-certs", work], timeout=300)
                dg = re.search(r"SHA-256 digest:\s*([0-9a-f]+)", rv["stdout"])
                d = dg.group(1) if dg else "?"
                digests[os.path.basename(work)] = d
                report.append(f"{os.path.basename(work)}: signed OK, "
                              f"{os.path.getsize(work)} bytes, cert SHA-256 {d[:16]}...")
            uniq = set(digests.values())
            verdict = ("\nAll certificates MATCH -> installable as one split set."
                       if len(uniq) == 1 else
                       f"\nWARNING: {len(uniq)} different certificates! install-multiple will fail."
                       if len(uniq) > 1 else "")
            return err(f"keystore: {ks} (alias {alias})\n" + "\n".join(report) + verdict)

        if name == "apk_info":
            apk = abs_path(a["apk_path"])
            if not os.path.exists(apk):
                return err(f"no such file {apk}")
            out = [f"file: {apk} ({os.path.getsize(apk)} bytes)"]
            aapt = which("aapt2") or which("aapt")
            if aapt:
                rb = await run_cmd([aapt, "dump", "badging", apk], timeout=180)
                for line in rb["stdout"].splitlines():
                    if line.startswith(("package:", "sdkVersion:", "targetSdkVersion:",
                                        "application-label:", "split:", "native-code:",
                                        "uses-permission:", "launchable-activity:")):
                        out.append(line.strip())
                if not a.get("verbose"):
                    out = [l for l in out if not l.startswith("uses-permission:")] + \
                          [f"permissions: {rb['stdout'].count('uses-permission:')}"]
            rz = await run_cmd(f"unzip -l {json.dumps(apk)} | tail -1", timeout=120)
            out.append("zip: " + rz["stdout"].strip())
            rl = await run_cmd(f"unzip -l {json.dumps(apk)} | grep -o 'lib/[^/]*' | sort -u", timeout=120)
            out.append("native ABIs: " + (rl["stdout"].replace(chr(10), " ").strip() or "none"))
            rm = await run_cmd(f"unzip -p {json.dumps(apk)} AndroidManifest.xml 2>/dev/null | head -c 0 ; echo ok", timeout=60)
            signer = which("apksigner")
            if signer:
                rv = await run_cmd([signer, "verify", "--print-certs", apk], timeout=300)
                dg = re.search(r"SHA-256 digest:\s*([0-9a-f]+)", rv["stdout"])
                out.append("signature: " + (f"cert SHA-256 {dg.group(1)}" if dg
                                            else (rv["stderr"].strip()[:200] or "unsigned?")))
            return err("\n".join(out))

        if name == "apk_replace_entry":
            apk = abs_path(a["apk_path"])
            entry = a["entry"].lstrip("/")
            src = abs_path(a["source_file"])
            if not os.path.exists(apk) or not os.path.exists(src):
                return err("apk or source_file missing")
            stage = os.path.join(WORK_DIR, ".stage_" + uuid.uuid4().hex[:6])
            dst = os.path.join(stage, entry)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            tool = which("zip")
            if tool:
                r = await run_cmd(f"cd {json.dumps(stage)} && {tool} -q {json.dumps(apk)} {json.dumps(entry)}", timeout=600)
            else:
                jar = which("jar") or "jar"
                r = await run_cmd([jar, "uf", apk, "-C", stage, entry], timeout=600)
            shutil.rmtree(stage, ignore_errors=True)
            chk = await run_cmd(f"unzip -l {json.dumps(apk)} | grep -F {json.dumps(entry)}", timeout=120)
            return err(f"Replaced {entry} in {apk}\nEXIT: {r['returncode']}\n"
                       f"{r['stderr'][:300]}\nentry now: {chk['stdout'].strip()}\n"
                       f"NOTE: re-sign the APK afterwards (sign_apk / apk_sign_all).")

        # ================= emulator =================
        if name == "start_emulator":
            model = a.get("model", "Pixel7")
            version = a.get("version", 33)
            max_time = a.get("max_time", "1h")
            cmd = (f"nohup {EW_CLI} start-session --device model={model},version={version} "
                   f"--adb --json --max-time-limit {max_time} > /workspace/ew-session.log 2>&1 &\n"
                   f"echo PID: $!\nsleep 12\ncat /workspace/ew-session.log")
            r = await run_cmd(cmd, timeout=60)
            text = f"Start emulator (model={model}, version={version}, max_time={max_time})\n{r['stdout']}\n{r['stderr']}"
            if a.get("wait_boot", True):
                booted = False
                for _ in range(30):
                    await asyncio.sleep(5)
                    s = get_adb_serial()
                    if not s:
                        continue
                    rb = await run_cmd(["adb", "-s", s, "shell", "getprop", "sys.boot_completed"], timeout=20)
                    if rb["stdout"].strip() == "1":
                        booted = True
                        text += f"\nBOOT COMPLETED, serial={s}"
                        break
                if not booted:
                    text += "\nWARNING: boot not confirmed within ~150s; check emulator_status."
            return err(text)

        if name == "stop_emulator":
            r = await run_cmd([EW_CLI, "stop-session"], timeout=120)
            return fmt(r, "Stop emulator")

        if name == "emulator_status":
            rd = await run_cmd(["adb", "devices", "-l"], timeout=30)
            s = get_adb_serial()
            info = [f"adb devices:\n{rd['stdout'].strip()}", f"detected serial: {s}"]
            if s:
                for prop, label in (("sys.boot_completed", "boot_completed"),
                                    ("ro.product.cpu.abilist", "abilist"),
                                    ("ro.build.version.sdk", "sdk"),
                                    ("ro.product.model", "model")):
                    rp = await run_cmd(["adb", "-s", s, "shell", "getprop", prop], timeout=20)
                    info.append(f"{label}: {rp['stdout'].strip()}")
                rw = await run_cmd(["adb", "-s", s, "shell", "wm", "size"], timeout=20)
                rdn = await run_cmd(["adb", "-s", s, "shell", "wm", "density"], timeout=20)
                info.append(rw["stdout"].strip() + " | " + rdn["stdout"].strip())
            rl = await run_cmd("tail -20 /workspace/ew-session.log 2>/dev/null", timeout=20)
            info.append("session log tail:\n" + (rl["stdout"].strip() or "(none)"))
            return err("\n".join(info))

        # ================= adb =================
        if name == "adb_command":
            if a.get("shell_string"):
                r, s = await adb_shell_str(a["shell_string"], serial, a.get("timeout", 60))
                return fmt(r, f"adb shell (verbatim): {a['shell_string']}")
            r, s = await adb(a["args"].split(), serial, a.get("timeout", 60))
            return fmt(r, f"adb {'-s ' + s + ' ' if s else ''}{a['args']}")

        if name == "adb_install":
            apk = abs_path(a["apk_path"])
            args = ["install", "-r", "-t"] + (["-d"] if a.get("downgrade") else []) + [apk]
            r, s = await adb(args, serial, timeout=600)
            return fmt(r, f"Install {apk}")

        if name == "adb_install_multiple":
            apks = [abs_path(x) for x in a["apk_paths"]]
            missing = [p for p in apks if not os.path.exists(p)]
            if missing:
                return err("missing files: " + ", ".join(missing))
            args = ["install-multiple"] + (["-r"] if a.get("reinstall", True) else []) + ["-t"] + apks
            r, s = await adb(args, serial, timeout=900)
            hint = ""
            if "INSTALL_FAILED_MISSING_SPLIT" in (r["stdout"] + r["stderr"]):
                hint = ("\nHINT: the set is incomplete. Include the base APK plus the abi "
                        "(config.arm64_v8a / config.x86_64), density and language splits.")
            if "INSTALL_FAILED_UPDATE_INCOMPATIBLE" in (r["stdout"] + r["stderr"]):
                hint = "\nHINT: an existing copy is signed with a different key. Uninstall it first."
            return fmt(r, f"install-multiple ({len(apks)} apks)" ) if not hint else \
                err(f"install-multiple ({len(apks)} apks)\n{r['stdout']}\n{r['stderr']}\nEXIT: {r['returncode']}{hint}")

        if name == "adb_logcat":
            if a.get("clear_first"):
                await adb(["logcat", "-c"], serial, timeout=30)
                return err("logcat buffer cleared. Reproduce the issue, then call adb_logcat again.")
            args = ["logcat", "-d", "-t", str(a.get("lines", 100))]
            if a.get("filter"):
                args += a["filter"].split()
            pre, s = adb_prefix(serial)
            cmd = " ".join(pre + args)
            if a.get("grep"):
                cmd += " | grep -i " + json.dumps(a["grep"])
            r = await run_cmd(cmd, timeout=90)
            return err(r["stdout"] or r["stderr"] or "(no output)")

        if name == "adb_screenshot":
            out = abs_path(a.get("output_path") or "screenshot.png")
            png, path, e = await capture_png(serial, out)
            return err(e or f"Screenshot saved -> {path} ({len(png)} bytes). "
                            f"Use screen_view to actually see it.")

        # ================= vision + control =================
        if name == "screen_view":
            return await screen_content(serial, a.get("max_width", 720), a.get("grid", False),
                                        a.get("jpeg_quality"),
                                        abs_path(a["save_path"]) if a.get("save_path") else None)

        if name == "ui_dump":
            xml, e = await fetch_hierarchy(serial)
            if e:
                return err(e)
            nodes, pe = walk_nodes(xml)
            if pe:
                return err("parse error: " + pe)
            if a.get("contains"):
                q = a["contains"].lower()
                nodes = [n for n in nodes if q in (n["text"] + n["desc"] + n["id"]).lower()]
            lines = render_nodes(nodes, not a.get("all_nodes"))
            webview = [n for n in nodes if "WebView" in n["class"]]
            note = ""
            if webview and len(lines) < 6:
                note = ("\nNOTE: this screen is rendered inside a WebView, so its real content is "
                        "invisible to uiautomator. Use screen_view (pixels) or webview_eval (DOM).")
            return err(f"{len(nodes)} nodes, showing {len(lines)}\n" + "\n".join(lines) + note)

        if name in ("ui_tap", "ui_swipe", "ui_text", "ui_key", "ui_find_tap"):
            note = ""
            if name == "ui_tap":
                r, s = await adb(["shell", "input", "tap", str(a["x"]), str(a["y"])], serial)
                note = f"tapped ({a['x']},{a['y']}) exit={r['returncode']}"
            elif name == "ui_swipe":
                r, s = await adb(["shell", "input", "swipe", str(a["x1"]), str(a["y1"]),
                                  str(a["x2"]), str(a["y2"]), str(a.get("duration_ms", 400))], serial)
                note = (f"swiped ({a['x1']},{a['y1']})->({a['x2']},{a['y2']}) "
                        f"in {a.get('duration_ms', 400)}ms exit={r['returncode']}")
            elif name == "ui_text":
                r, s = await adb_shell_str("input text " + json.dumps(a["text"]), serial)
                note = f"typed {len(a['text'])} chars exit={r['returncode']}"
            elif name == "ui_key":
                k = norm_key(a["key"])
                r, s = await adb(["shell", "input", "keyevent", k], serial)
                note = f"key {k} exit={r['returncode']}"
            else:  # ui_find_tap
                xml, e = await fetch_hierarchy(serial)
                if e:
                    return err(e)
                nodes, pe = walk_nodes(xml)
                q = a["query"].lower()
                hits = [n for n in nodes
                        if q in (n["text"] + " " + n["desc"] + " " + n["id"]).lower()
                        and center(n["bounds"])]
                if not hits:
                    return err(f"No element matching {a['query']!r}. "
                               f"Run ui_dump to see what is on screen "
                               f"(WebView content will not appear).")
                idx = min(a.get("index", 0), len(hits) - 1)
                n = hits[idx]
                c = center(n["bounds"])
                r, s = await adb(["shell", "input", "tap", str(c[0]), str(c[1])], serial)
                note = (f"matched {len(hits)} element(s), tapped #{idx}: "
                        f"{n['class'].split('.')[-1]} text={n['text']!r} desc={n['desc']!r} "
                        f"id={n['id']} at {c} exit={r['returncode']}")
            if a.get("view_after"):
                await asyncio.sleep(max(0, a.get("delay_ms", 700)) / 1000.0)
                return await screen_content(serial, 720, False, None, None, note)
            return err(note)

        if name == "app_control":
            act = a.get("action", "").lower()
            pkg = a.get("package")
            if act == "current":
                r, s = await adb_shell_str(
                    "dumpsys activity activities | grep -E 'topResumedActivity|mResumedActivity' "
                    "; dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'", serial)
                return err(r["stdout"] or r["stderr"])
            if not pkg:
                return err("package is required for this action")
            if act == "launch":
                if a.get("activity"):
                    r, s = await adb(["shell", "am", "start", "-n", f"{pkg}/{a['activity']}"], serial)
                else:
                    r, s = await adb(["shell", "monkey", "-p", pkg, "-c",
                                      "android.intent.category.LAUNCHER", "1"], serial)
            elif act == "stop":
                r, s = await adb(["shell", "am", "force-stop", pkg], serial)
            elif act == "clear":
                r, s = await adb(["shell", "pm", "clear", pkg], serial)
            elif act == "info":
                r, s = await adb_shell_str(
                    f"pidof {pkg} ; dumpsys package {pkg} | grep -E "
                    f"'versionName|versionCode|firstInstallTime|lastUpdateTime|splits|codePath'", serial)
            else:
                return err("action must be launch|stop|clear|info|current")
            return fmt(r, f"app_control {act} {pkg}")

        # ================= WebView / CDP =================
        if name in ("devtools_targets", "webview_eval"):
            port = a.get("port", 9222)
            sock, e = await cdp_setup(serial, port, a.get("socket"))
            if e:
                return err(e)
            try:
                targets = await cdp_list(port)
            except Exception as ex:
                return err(f"forwarded socket {sock} on tcp:{port} but /json/list failed: {ex}")
            if name == "devtools_targets":
                rows = [f"socket {sock} -> tcp:{port}", f"{len(targets)} target(s):"]
                for i, t in enumerate(targets):
                    rows.append(f"[{i}] {t.get('type')} | {t.get('title', '')[:70]} | "
                                f"{t.get('url', '')[:90]}")
                return err("\n".join(rows))
            if not targets:
                return err(f"socket {sock} forwarded but no CDP targets found.")
            ti = min(a.get("target_index", 0), len(targets) - 1)
            ws_url = targets[ti].get("webSocketDebuggerUrl")
            if not ws_url:
                return err(f"target #{ti} has no webSocketDebuggerUrl (already attached?)")
            try:
                msg = await cdp_eval(ws_url, a["expression"], a.get("timeout", 30))
            except Exception as ex:
                return err(f"CDP evaluate failed: {type(ex).__name__}: {ex}")
            res = msg.get("result", {})
            if res.get("exceptionDetails"):
                return err("JS exception:\n" + json.dumps(res["exceptionDetails"], indent=2)[:4000])
            val = res.get("result", {})
            payload = val.get("value", val.get("description", val))
            if not isinstance(payload, str):
                payload = json.dumps(payload, indent=2, default=str)
            return err(f"target[{ti}] {targets[ti].get('url', '')[:80]}\n"
                       f"type={val.get('type')}\n{payload[:12000]}")

        # ================= performance =================
        if name == "perf_gfxinfo":
            pkg = a["package"]
            if a.get("reset"):
                r, s = await adb(["shell", "dumpsys", "gfxinfo", pkg, "reset"], serial, timeout=90)
                return err(f"gfxinfo counters reset for {pkg}. Now interact with the app "
                           f"(scroll a long thread), then call perf_gfxinfo again without reset.")
            args = ["shell", "dumpsys", "gfxinfo", pkg]
            if a.get("framestats"):
                args.append("framestats")
            r, s = await adb(args, serial, timeout=120)
            if r["returncode"] != 0 or not r["stdout"].strip():
                return fmt(r, f"gfxinfo {pkg}")
            return err(f"=== gfxinfo {pkg} ===\n{parse_gfxinfo(r['stdout'])}\n\n"
                       f"(Janky frames % is the lag metric. >5% is perceptible, "
                       f"99th percentile above 16.7ms means dropped frames.)")

        if name == "perf_meminfo":
            r, s = await adb(["shell", "dumpsys", "meminfo", a["package"]], serial, timeout=120)
            if not r["stdout"].strip():
                return fmt(r, f"meminfo {a['package']}")
            return err(f"=== meminfo {a['package']} ===\n{parse_meminfo(r['stdout'])}")

        return err(f"Unknown tool: {name}")

    except KeyError as e:
        return err(f"Missing required argument: {e}")
    except Exception as e:
        return err(f"Tool '{name}' raised {type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}")


# ----------------------------------------------------------------------------
# HTTP transport (unchanged from v13.1 - proven stable)
# ----------------------------------------------------------------------------

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
    wrapped_send = make_send_wrapper(send)
    try:
        print("SSE: GET connection started at /sse", flush=True)
        async with sse.connect_sse(scope, receive, wrapped_send) as (read, write):
            print("SSE: streams connected, starting server.run()", flush=True)
            await server.run(read, write, server.create_initialization_options())
            print("SSE: server.run() completed", flush=True)
    except Exception as e:
        errtb = traceback.format_exc()
        print(f"SSE Error: {errtb}", flush=True)
        try:
            body = json.dumps({"error": str(e), "traceback": errtb}).encode()
            await send({"type": "http.response.start", "status": 500, "headers": JSON_HEADERS})
            await send({"type": "http.response.body", "body": body})
        except Exception:
            pass


class ASGIApp:
    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return
        path = scope["path"]
        method = scope["method"]

        if method == "OPTIONS":
            await send({"type": "http.response.start", "status": 200, "headers": CORS_HEADERS})
            await send({"type": "http.response.body", "body": b""})
            return

        if path in ("/sse", "/") and method == "HEAD":
            await send({"type": "http.response.start", "status": 200, "headers": SSE_HEADERS})
            await send({"type": "http.response.body", "body": b""})
            return

        if path == "/" and method in ("POST", "GET", "DELETE") and HAS_STREAMABLE:
            await init_streamable()
            if _streamable_transport:
                try:
                    await _streamable_transport.handle_request(scope, receive, send)
                    return
                except Exception as e:
                    print(f"Streamable HTTP error: {e}", flush=True)
                    traceback.print_exc()

        if path in ("/sse", "/") and method == "GET":
            await handle_sse(scope, receive, send)
            return

        if path == "/.well-known/mcp.json" and method in ("GET", "HEAD"):
            body = json.dumps({
                "name": "android-re-mcp",
                "version": VERSION,
                "transports": {"sse": "/sse", "streamable_http": "/"}
            }).encode()
            await send({"type": "http.response.start", "status": 200, "headers": JSON_HEADERS})
            await send({"type": "http.response.body", "body": body if method == "GET" else b""})
            return

        if path == "/.well-known/oauth-protected-resource/sse" and method in ("GET", "HEAD"):
            body = json.dumps({
                "resource": "https://docker-1be-8080.ny1.zerops.app/sse",
                "authorization_servers": [], "bearer_methods": ["header"],
                "scopes_supported": []}).encode()
            await send({"type": "http.response.start", "status": 200, "headers": JSON_HEADERS})
            await send({"type": "http.response.body", "body": body if method == "GET" else b""})
            return

        if path == "/.well-known/oauth-protected-resource" and method in ("GET", "HEAD"):
            body = json.dumps({
                "resource": "https://docker-1be-8080.ny1.zerops.app",
                "authorization_servers": [], "bearer_methods": ["header"],
                "scopes_supported": []}).encode()
            await send({"type": "http.response.start", "status": 200, "headers": JSON_HEADERS})
            await send({"type": "http.response.body", "body": body if method == "GET" else b""})
            return

        if path == "/.well-known/oauth-authorization-server" and method in ("GET", "HEAD"):
            await send({"type": "http.response.start", "status": 404, "headers": JSON_HEADERS})
            await send({"type": "http.response.body", "body": b""})
            return

        if path == "/.well-known/openid-configuration" and method in ("GET", "HEAD"):
            await send({"type": "http.response.start", "status": 404, "headers": JSON_HEADERS})
            await send({"type": "http.response.body", "body": b"{\"error\":\"no_openid_support\"}"})
            return

        if path == "/health" and method == "GET":
            try:
                n_tools = len(await list_tools())
            except Exception:
                n_tools = -1
            body = json.dumps({
                "status": "ok", "tools": n_tools, "version": VERSION,
                "streamable": HAS_STREAMABLE,
                "capabilities": {
                    "vision": HAS_PIL, "websocket_cdp": HAS_WS,
                    "adb": bool(shutil.which("adb")),
                    "apksigner": bool(which("apksigner")),
                    "zipalign": bool(which("zipalign")),
                    "aapt2": bool(which("aapt2")),
                },
            }).encode()
            await send({"type": "http.response.start", "status": 200, "headers": JSON_HEADERS})
            await send({"type": "http.response.body", "body": body})
            return

        if path.startswith("/messages/") and method == "POST":
            await sse.handle_post_message(scope, receive, send)
            return

        await send({"type": "http.response.start", "status": 404,
                    "headers": [[b"content-type", b"text/plain"]]})
        await send({"type": "http.response.body", "body": b"Not Found"})


if __name__ == "__main__":
    print(f"Android RE MCP {VERSION} starting (PIL={HAS_PIL}, WS={HAS_WS}, "
          f"streamable={HAS_STREAMABLE})", flush=True)
    uvicorn.run(ASGIApp(), host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
