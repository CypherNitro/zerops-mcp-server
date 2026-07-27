# zerops-mcp-server

Android reverse-engineering MCP server (Zerops + emulator.wtf) - **v14.0 "powerhouse", 37 tools**.

## What v14 adds over v13.1 (15 tools)

### Vision + control (the headline)
| Tool | Purpose |
|---|---|
| `screen_view` | Captures the screen and returns it as a real **image** so a vision model can SEE it. `grid=true` overlays device-pixel coordinates for accurate tapping. `max_width` / `jpeg_quality` control token cost. |
| `ui_dump` | View hierarchy as compact text with a ready-to-use tap coordinate per node. Warns when the screen is a WebView (content invisible to uiautomator). |
| `ui_tap`, `ui_swipe`, `ui_text`, `ui_key` | Full input control. All accept `view_after=true` to return a screenshot of the result, enabling a see-act-see loop. |
| `ui_find_tap` | Find an element by text / content-desc / resource-id and tap its center. |
| `app_control` | launch / stop / clear / info / current-foreground-activity. |

### WebView introspection (Chrome DevTools Protocol)
| Tool | Purpose |
|---|---|
| `devtools_targets` | Auto-forwards the device devtools socket and lists CDP targets. |
| `webview_eval` | Evaluates JavaScript **inside the live WebView**: inspect the DOM, measure render timing, read app state. Requires a debuggable WebView. |

### Performance profiling
| Tool | Purpose |
|---|---|
| `perf_gfxinfo` | The lag metric: total frames, **janky frame %**, and 50/90/95/99th percentile frame times. Use `reset=true`, interact, then read again. |
| `perf_meminfo` | Total PSS, native/dalvik heap, graphics memory, view and WebView counts. |

### Reliability: background jobs
`job_start` / `job_status` / `job_logs` / `job_list` run long operations (decompile, download, rebuild) in the background and return immediately, which avoids MCP request timeouts. `decompile_apk` and `rebuild_apk` accept `async_job=true`.

### Split-APK aware tooling
| Tool | Purpose |
|---|---|
| `adb_install_multiple` | Installs a base + config split set together - required for App Bundle apps. Explains `INSTALL_FAILED_MISSING_SPLIT` and signature-mismatch failures. |
| `apk_sign_all` | Zipaligns and signs a whole split set with one keystore, then verifies every certificate digest matches. |
| `apk_info` | Package, version, sdk levels, split name, permissions, native ABIs, entry count, signing certificate. |
| `apk_replace_entry` | Replaces one file inside an APK without a full decompile/rebuild - ideal for patching bundled JS/assets. |

### Quality-of-life
- `emulator_status` (boot state, ABI list, screen size, session log) and `start_emulator` with `wait_boot`.
- `read_file` with line `offset`/`limit` and `binary` (base64) mode.
- `search_in_files` with context lines, fixed-string mode and result caps.
- `http_request` for arbitrary HTTP calls; `write_file` supports `append`.
- `adb_command` gained `shell_string` for device commands needing pipes/quotes.
- `/health` now reports live capability detection (vision, cdp, apksigner, zipalign, aapt2).

## Baked-in toolchain
Debian packages: `zip`, `curl`, `binutils` (strings), `vim-common` (xxd), `procps` (ps), plus JDK, git, unzip, file.
Android: platform-tools (adb), **build-tools r34** (`zipalign`, `apksigner`, `aapt2`), apktool 2.9.3, ew-cli.
Python: mcp, starlette, uvicorn, httpx, **pillow** (vision), **websockets** (CDP), requests.

## Deploy
Zerops builds the image from this repo's `main` branch (`zerops.yml`), then runs it on port 8080.
Endpoints: `/sse` (SSE), `/` (Streamable HTTP), `/health`.
