# Decky Cloud Reader Plugin

## Project Overview

This is a **Decky Loader plugin** for Steam Deck. It is a classic Decky plugin that runs within the Decky Loader framework.

**Purpose:** Use GCP services (Cloud Vision OCR + Cloud Text-to-Speech) to provide OCR and TTS functionality for text-heavy games on Steam Deck.

## Development Environment

- **Host machine:** M1 MacBook Pro (ARM / Apple Silicon)
- **Target device:** Steam Deck at IP `192.168.50.116` with SSH configured and passwordless sudo
- **Build/test architecture:** All local testing and builds must be done inside an **x86 Docker image** with **Python 3.13** (matching the Steam Deck's Python version) before deploying to the Deck

## Development Workflow

- Work in **small incremental steps**, testing each change immediately on the target Steam Deck
- Build and validate locally in the x86 Docker container first
- Deploy to Steam Deck via SSH for on-device testing
- **Comment code extensively** and provide detailed explanations in output — treat me as someone unfamiliar with the structures, approaches, frameworks, and programming languages being used, so I can learn as we go
- **Challenge vague requests** — if I ask for something in vague terms, don't just execute it blindly. Question my decision, propose better alternatives if they exist, and ask clarifying questions to gather enough context before proceeding

## Reference Projects (Local Clones)

### Decky Plugin Template (primary reference)
- **Path:** `/Users/mshabalov/Documents/claude-projects/decky-plugin-template`
- Use as the main structural reference for plugin scaffolding, build system, and conventions

### Decky-Translator (UI and input reference)
- **Path:** `/Users/mshabalov/Documents/claude-projects/Decky-Translator`
- Reference for:
  - Using the **L4 button** on Steam Deck to trigger plugin actions without opening the plugin UI

### decky-ocr-tts-claude-service-plugin (feature reference)
- **Path:** `/Users/mshabalov/Documents/claude-projects/decky-ocr-tts-claude-service-plugin`
- Contains a **working GCP + OCR + TTS plugin** implementation
- **Architecture note:** This plugin uses a separate Python service, which is NOT the desired architecture for our new plugin
- **Useful for:** Borrowing UI features and Python OCR/TTS logic to adapt into our integrated implementation

---

## Implementation Plan & Progress

### Architecture Overview

Everything runs inside the standard Decky plugin process — no separate service. The Python backend (`main.py`) handles screen capture, worker management, and audio playback. GCP API calls are delegated to a **persistent** `gcp_worker.py` subprocess running under system Python. The TypeScript frontend (`src/index.tsx`) provides the UI panel with settings and status.

```
Frontend (TypeScript/React)           Backend (Python)
┌──────────────────────────┐         ┌───────────────────────────────┐
│ Decky Panel UI           │   RPC   │ main.py (Plugin class)        │
│  - Read Screen (primary) │◄───────►│  - Pipeline orchestration     │
│  - Credentials section   │         │  - GCP credentials mgmt       │
│  - Settings section      │         │  - Screen capture (GStreamer) │
│  - Button trigger config │         │  - Worker lifecycle mgmt      │
│  - OCR/TTS controls      │         │  - Audio playback (Popen)     │
│  - Enabled toggle gates  │         │  - Enabled toggle teardown    │
│    GCP-dependent buttons │         │    (worker + playback + pipe) │
│                          │         │                               │
│ Global Overlay           │         │  hidraw_monitor.py (thread)   │
│  - OCR text display      │         │  - Button hold detection      │
│                          │         │  - Auto-reconnect             │
└──────────────────────────┘         │                               │
                                     │  Persistent subprocess        │
                                     │  ┌──────────────────────────┐ │
                                     │  │ gcp_worker.py (serve)    │ │
                                     │  │  - stdin/stdout JSON     │ │
                                     │  │  - Pre-init GCP clients  │ │
                                     │  │  - OCR (Cloud Vision)    │ │
                                     │  │  - TTS (Cloud TTS)       │ │
                                     │  │  - Warm gRPC connections │ │
                                     │  └──────────────────────────┘ │
                                     └───────────────────────────────┘
```

### Phase 1: Foundation & Build Pipeline `[DONE]`
- [x] Plugin scaffolding (package.json, plugin.json, tsconfig, rollup config)
- [x] Basic Python backend with lifecycle hooks (main.py)
- [x] Basic TypeScript frontend with test button (src/index.tsx)
- [x] Docker-based x86 build system (docker/Dockerfile.plugin, docker-compose.yml)
- [x] SSH deploy script to Steam Deck (deploy.sh)
- [x] Frontend-backend RPC communication working (get_greeting test)

### Phase 2: Settings & Credential Management `[DONE]`
- [x] Create `requirements.txt` with `google-cloud-vision`, `google-cloud-texttospeech`, `Pillow`
- [x] Implement backend settings manager (read/write JSON to DECKY_PLUGIN_SETTINGS_DIR)
- [x] Implement GCP credential storage (base64-encoded service account key in settings)
- [x] File browser UI for navigating filesystem and selecting credential JSON file
- [x] Credential validation (required GCP service account fields + type check)
- [x] Settings UI with Enabled/Debug toggles, credential status display
- [x] Build and deploy to verify Python dependencies install correctly

### Phase 3: Screen Capture `[DONE]`
- [x] Implement GStreamer/PipeWire screenshot capture in Python backend
- [x] Add `capture_screenshot()` RPC method
- [x] Test screenshot capture on Steam Deck (verify GStreamer + PipeWire work)
- [x] Add "Test Capture" button in UI to verify

### Phase 4: OCR — Cloud Vision (subprocess) `[DONE]`

**Subprocess infrastructure:**
- [x] Create `gcp_worker.py` — standalone script that runs under system Python (`/usr/bin/python3`), receives commands via CLI args, outputs JSON to stdout
- [x] System Python discovery in `_main()` — locate `/usr/bin/python3` (or fallback paths), validate version, warn and degrade gracefully if not found
- [x] Subprocess launcher helper `_run_gcp_worker(action, args, timeout)` — sets `PYTHONPATH` to `py_modules/`, sets `PYTHONNOUSERSITE=1`, runs with `subprocess.run()` + timeout, parses JSON stdout
- [x] Subprocess hygiene: always use `subprocess.run()` (not Popen) for request-response calls — it waits for exit, so no zombies; enforce timeout to prevent hangs; clean up temp files in `finally` blocks

**OCR logic (inside `gcp_worker.py`):**
- [x] Vision client init from base64 credentials (passed via env var, not CLI — avoids `ps` exposure)
- [x] Image resize if >10MB (Pillow, two-stage: JPEG quality → dimension scaling)
- [x] `text_detection()` call with retry (3 attempts, backoff on 503/429/timeout)
- [x] Output: JSON `{success, text, char_count, line_count, message}` to stdout; errors/logs to stderr

**RPC + Frontend:**
- [x] `perform_ocr()` RPC: capture screenshot → write to temp file → `_run_gcp_worker("ocr", ...)` → return result
- [x] Frontend: "Test OCR" button, status message, scrollable text display

**Note:** Docker base image changed from `node:20-slim` to `python:3.13-slim` (with Node.js installed on top) to match Steam Deck's Python 3.13 — required for compatible C extensions (.so files).

### Phase 5: TTS — Cloud Text-to-Speech (subprocess + audio playback) `[DONE]`

**TTS logic (inside `gcp_worker.py`):**
- [x] TTS client init from base64 credentials (same pattern as Vision client)
- [x] TTS synthesis action: receives text + voice config via CLI args, calls Cloud TTS API, writes MP3 to output path
- [x] Voice registry (8 Neural2 voices: 4 US, 4 UK) + speech rate presets (x-slow through x-fast as floats)
- [x] Text truncation at 5000 chars (Cloud TTS API limit) with "(text truncated)" note
- [x] Retry logic (same pattern as OCR — 3 attempts, backoff on 503/429/timeout)
- [x] Output: JSON `{success, audio_size, output_path, text_length, voice_id, message}` to stdout

**Audio playback (multi-player, long-running, uses Popen):**
- [x] Audio player discovery in `_main()` — tries mpv → ffplay → pw-play, stores path + name
- [x] `_start_playback(mp3_path)` — builds player-specific command (mpv/ffplay/pw-play each have different flags), launches via Popen with `XDG_RUNTIME_DIR=/run/user/1000` (required since Decky runs as root)
- [x] `_stop_playback()` — SIGTERM → `wait(timeout=2)` → SIGKILL if needed, catches `ProcessLookupError` for already-exited process
- [x] Playback state tracking: `self._playback_process` checked via `poll()`; a daemon reaper thread (`_reap_playback`) calls `wait()` on each player process to prevent zombies when playback finishes naturally with the UI panel closed
- [x] `_cleanup_tts_temp()` helper: removes temp MP3 after playback stops
- [x] `_unload()` cleanup: stop playback + sweep orphaned `/tmp/dcr_*.png` and `/tmp/dcr_*.mp3`

**RPC + Frontend:**
- [x] `perform_tts(text)`, `stop_playback()`, `get_playback_status()` RPC methods
- [x] Settings UI — voice dropdown (8 Neural2 voices), speech rate dropdown (5 presets), volume slider (0-100, debounced save)
- [x] "Read Text" / "Stop Playback" toggle button with icons, playback status polling (1s interval)
- [x] Contextual hints: "Run OCR first" when no text, "Load GCP credentials" when not configured

**Note:** mpv is not pre-installed on Steam Deck. ffplay (from ffmpeg) is the primary audio player found on the device. The plugin auto-discovers the best available player at startup.

### Phase 6: End-to-End OCR+TTS Pipeline `[DONE]`

**Pipeline orchestration:**
- [x] `read_screen()` RPC: capture → `_run_gcp_worker("ocr_tts", ...)` → `_start_playback()` (later replaced by `_send_to_worker()` in Phase 6.5)
- [x] Combined `ocr_tts` subprocess action: OCR+TTS in single process, sharing Python startup, imports, and credential decode (~2-3s faster than separate calls)
- [x] Each step checks for cancellation flag (`self._pipeline_cancel`) before proceeding
- [x] `stop_pipeline()` RPC: sets cancel flag + stops any running playback
- [x] Loading/progress states in UI during pipeline execution (`get_pipeline_status()` polled at 1s)

**Subprocess lifecycle guarantees (before Phase 6.5 persistent worker):**
- [x] `_unload()` sets pipeline cancel flag + stops playback + sweeps orphaned temp files
- [x] Temp file cleanup in `_read_screen_sync()` finally block — OCR temp always cleaned, TTS temp only if playback didn't start
- [x] No Popen without corresponding `wait()` — the golden rule against zombies (relaxed in Phase 6.5 for the persistent worker, which uses its own shutdown protocol)

**Frontend (Read Screen section):**
- [x] "Read Screen" / "Stop" toggle button with icons (FaBook / FaStop), placed as first section in panel
- [x] Pipeline progress indicator (step labels: Capturing → Detecting text → Generating speech → Playing)
- [x] OCR text populated in existing scrollable display even if TTS fails
- [x] Standalone buttons (Test Capture, Test OCR, Read Text) disabled while pipeline is running
- [x] Concurrent pipeline rejection ("Pipeline already running")

### Phase 6.5: Persistent GCP Worker Subprocess `[DONE]`

**Problem:** After Phase 6, the end-to-end pipeline took ~5.2s. Instrumented timing inside the subprocess revealed that ~1.7s was wasted on per-call overhead (Python startup + google-cloud imports + GCP client initialization + subprocess spawn) and the TTS API call took ~2.8s partly because every call opened a fresh gRPC connection. The combined `ocr_tts` action helped by sharing imports and credential decode within one call, but couldn't eliminate the overhead of spawning a new process every time.

**Solution:** Convert `gcp_worker.py` from a one-shot CLI tool into a persistent subprocess that stays alive between requests. Initialize Python imports and GCP API clients **once** at startup, then communicate via stdin/stdout JSON lines. gRPC connections stay warm across requests (HTTP/2 reuse).

**Timing improvement:**
| Metric | Before (Phase 6) | After (Phase 6.5) |
|--------|-------------------|--------------------|
| 1st trigger (cold worker) | 5.2s | 4.7s |
| 2nd+ trigger (warm worker) | 5.2s | 3.2s |
| Per-call overhead eliminated | — | ~1.5s (imports + client init + subprocess spawn) |

**gcp_worker.py refactoring:**
- [x] Exception-based flow control: `WorkerResult`/`WorkerError` exceptions replace `sys.exit()` in `output_result()`/`output_error()` — same `do_*` functions work in both CLI mode (exit after one call) and serve mode (continue looping)
- [x] Optional pre-initialized client parameters on `do_ocr(vision_client=)`, `do_tts(tts_client=)`, `do_ocr_tts(vision_client=, tts_client=)` — skip client creation when in serve mode
- [x] `serve()` function: reconfigure stdout for line buffering → read credentials → import all google-cloud libs → init both clients → send `{"ready": true}` → enter command loop
- [x] Command loop: read JSON from stdin → dispatch to `do_*` → catch `WorkerResult`/`WorkerError` → write JSON to stdout → continue
- [x] Shutdown: `{"action": "shutdown"}` command or stdin EOF
- [x] CLI `main()` dispatcher updated: wraps one-shot calls in try/except for `WorkerResult`/`WorkerError`, delegates to `serve()` for persistent mode

**main.py worker management:**
- [x] `_start_worker()`: launch `gcp_worker.py serve` via Popen (stdin/stdout/stderr PIPE), start stderr drain thread, wait for `{"ready": true}` with 30s timeout
- [x] `_stop_worker()`: send `{"action":"shutdown"}` → `wait(3)` → SIGTERM → `wait(2)` → SIGKILL, close all pipes
- [x] `_send_to_worker(command_dict, timeout)`: acquire `_worker_lock` → lazy start/restart if worker is dead → write JSON to stdin → read response with timeout thread → parse JSON → return dict
- [x] `_drain_worker_stderr()`: daemon thread reading stderr lines, logging via `decky.logger.debug` — prevents pipe buffer deadlock
- [x] All callers (`_perform_ocr_sync`, `_perform_tts_sync`, `_read_screen_sync`) migrated from `_run_gcp_worker()` to `_send_to_worker()`
- [x] `_run_gcp_worker()` removed entirely

**Lifecycle integration:**
- [x] `_main()`: initialize `_worker_process = None`, `_worker_lock`, `_worker_stderr_thread` (no eager start — lazy on first use)
- [x] `_unload()`: `_stop_worker()` early in cleanup (before temp file sweep)
- [x] `load_credentials_file()`: `_stop_worker()` after saving new credentials so next request lazy-starts with new creds
- [x] `clear_credentials()`: `_stop_worker()` before clearing the setting

### Phase 7: L4 Button Trigger `[DONE]`

**Hidraw button monitor (hidraw_monitor.py):**
- [x] HidrawButtonMonitor class — self-contained module adapted from Decky-Translator
- [x] Device discovery: scan `/sys/class/hidraw/`, match Valve VID `0x28DE` / PID `0x1205`, prefer interface `:1.2/`
- [x] HID initialization: disable lizard mode + trackpad emulation + watchdog via feature reports
- [x] Button masks for all Steam Deck buttons (L4/R4/L5/R5/L1/R1/A/B/X/Y/etc.)
- [x] Background daemon thread with `select()` polling (0.1s timeout for clean shutdown)
- [x] Hold-time detection: track press start, fire callback once when threshold met, 2s cooldown
- [x] Runtime reconfiguration via `configure()` — change target button / hold threshold without restart
- [x] Auto-reconnect: 10 errors → close device → 2s delay → reinitialize
- [x] Thread-safe state with `threading.Lock`

**Backend integration (main.py):**
- [x] Event loop capture in `_main()` for cross-thread async dispatch
- [x] Monitor lifecycle: create, start, stop in `_main()` / `_unload()`
- [x] Button trigger callback: `_on_button_trigger()` → `asyncio.run_coroutine_threadsafe()` → `_handle_button_trigger()`
- [x] Trigger guards: check enabled, not already running, credentials configured
- [x] Settings handling: `trigger_button` changes start/stop/reconfigure monitor; `hold_time_ms` updates threshold
- [x] `get_button_monitor_status()` RPC for UI status indicator
- [x] Graceful degradation: if hidraw device not found, plugin works via UI only

**Frontend (src/index.tsx):**
- [x] "Button Trigger" settings section between "Read Screen" and "GCP Credentials"
- [x] Trigger button dropdown: Disabled / L4 / R4 / L5 / R5
- [x] Hold time dropdown: 300ms / 500ms / 750ms / 1000ms / 1500ms
- [x] Status indicator: Connected / Not connected (fetched via `get_button_monitor_status()`)
- [x] Hint text explaining current configuration

### Phase 7.5: Enhanced Enabled Toggle `[DONE]`

**Problem:** The `enabled` toggle only gated the L4 button trigger callback. Toggling off had no side effects — the GCP worker subprocess stayed alive (holding memory + gRPC connections), audio playback continued, and GCP-dependent UI buttons remained active.

**Solution:** Make the toggle actively manage background resources and gate the UI.

**Backend (main.py):**
- [x] `_is_enabled` property — centralized check for the master switch, used by background trigger callbacks
- [x] `_handle_button_trigger()` uses `_is_enabled` instead of inline `settings.get()`
- [x] `save_setting("enabled", False)` handler: cancels running pipeline (`_pipeline_cancel.set()`), stops audio playback (`_stop_playback()`), shuts down GCP worker (`_stop_worker()`)
- [x] `save_setting("enabled", True)` handler: logs re-enable (worker lazy-starts on next use)
- [x] Updated `DEFAULT_SETTINGS` comment to describe full disable behavior

**Frontend (src/index.tsx):**
- [x] Read Screen button disabled when `!settings.enabled`
- [x] Test OCR button disabled when `!settings.enabled`
- [x] Read Text button disabled when `!settings.enabled`
- [x] Test Capture stays active (local screenshot, no GCP)
- [x] Toggle description updated: "Master switch — disables triggers and OCR/TTS"

**Design decisions:**
- Hidraw monitor keeps running when disabled — CPU cost is negligible, avoids HID re-init on re-enable, same pattern planned for future touchscreen monitor
- Worker lazy-starts on re-enable rather than eagerly — simpler and avoids unnecessary startup if user toggles on/off quickly

### Phase 8: UI Polish & Advanced Features `[NOT STARTED]`
- [ ] Global overlay for displaying OCR text on screen
- [ ] Region selection (crop to area instead of full screen)
- [ ] Text filtering (ignore specific words/patterns)
- [ ] Visual progress indicator during hold-to-activate
- [ ] Debug panel showing real-time state and diagnostics

---

## Logging Conventions

- **All backend log messages** use the `[DCR]` prefix so they stand out in the Decky Loader journal among logs from other plugins and the loader itself.
- Pattern: `decky.logger.info(f"{LOG} message here")` where `LOG = "[DCR]"` is defined at the top of `main.py`.
- Filter plugin logs on Steam Deck: `journalctl -u plugin_loader -f | grep DCR`
- **Debug Mode** (the `debug` setting toggle in the UI): when enabled, the backend calls `decky.logger.setLevel(logging.DEBUG)` so that `decky.logger.debug()` messages appear in the journal. The level is synced both at startup in `_main()` (from the saved setting) and at runtime in `save_setting()` when the user toggles the switch — no restart needed. Use debug-level logs for: RPC call parameters, settings reads/writes, directory listings, timing info, internal state. Use info-level logs for: lifecycle events, credential load/clear, errors.

---

## Key Technical Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Architecture | Single Decky plugin, GCP calls via persistent subprocess | main.py runs under Decky's embedded Python; gcp_worker.py runs under system Python (`/usr/bin/python3`) in persistent "serve" mode with `py_modules/` on PYTHONPATH |
| GCP worker mode | Persistent subprocess with stdin/stdout JSON lines | Eliminates ~1.7s per-call overhead (Python startup + imports + client init). GCP clients initialized once at startup; gRPC connections stay warm across requests |
| Screen capture | GStreamer + PipeWire | Native to Steam Deck, hardware-accelerated |
| Audio playback | ffplay (primary), mpv, or pw-play | Auto-discovered at startup; ffplay is reliably present on Steam Deck, mpv is not pre-installed. Requires `XDG_RUNTIME_DIR=/run/user/1000` since Decky runs as root. A daemon reaper thread calls `wait()` on the player process to prevent zombies when playback finishes naturally with the UI closed |
| GCP credentials | Base64-encoded service account JSON | Simple storage, same pattern as reference plugin |
| Button input | Hidraw direct device reading | Works in background without opening UI |
| Settings storage | JSON file in DECKY_PLUGIN_SETTINGS_DIR | Standard Decky convention |
| Pipeline optimization | Combined `ocr_tts` action via persistent worker | Read Screen pipeline sends single command to warm worker — no subprocess spawn, no imports, no client init |
| Pipeline cancellation | `threading.Event` checked between steps | Worker call timeout is bounded (60s); killing mid-request adds complexity for marginal benefit |
| Python deps | Bundled in py_modules/ via Docker build | Runs on Steam Deck without internet |

## File Structure

```
decky-cloud-reader/
├── src/
│   └── index.tsx              # Plugin entry, all UI (sections, file browser)
├── main.py                    # Python backend (lifecycle, RPC, pipeline, worker management)
├── hidraw_monitor.py          # Hidraw button monitor (hold-to-trigger, background thread)
├── gcp_worker.py              # GCP worker (persistent serve mode or one-shot CLI, system Python)
├── requirements.txt           # Python dependencies
├── package.json
├── plugin.json
├── tsconfig.json
├── rollup.config.js
├── docker/
│   ├── Dockerfile.plugin
│   └── docker-compose.yml
├── deploy.sh
└── CLAUDE.md
```

Components may be split out of `index.tsx` into separate files if it grows too large, but there's no predetermined file split — keep it simple until complexity demands it.
