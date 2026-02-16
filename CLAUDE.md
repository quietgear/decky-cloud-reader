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

Everything runs inside the standard Decky plugin process — no separate service. The Python backend (`main.py`) handles GCP API calls, screen capture, and audio playback. The TypeScript frontend (`src/index.tsx`) provides the UI panel with settings and status.

```
Frontend (TypeScript/React)         Backend (Python)
┌─────────────────────────┐        ┌──────────────────────────────┐
│ Decky Panel UI          │  RPC   │ main.py (Plugin class)       │
│  - Credentials section  │◄──────►│  - GCP credentials mgmt     │
│  - Settings section     │        │  - Screen capture (GStreamer)│
│  - Status/controls      │        │  - OCR (Cloud Vision)        │
│                         │        │  - TTS (Cloud TTS)           │
│ Global Overlay          │        │  - Audio playback (mpv)      │
│  - OCR text display     │        │  - L4 button monitor (hidraw)│
└─────────────────────────┘        └──────────────────────────────┘
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

### Phase 3: Screen Capture `[NOT STARTED]`
- [ ] Implement GStreamer/PipeWire screenshot capture in Python backend
- [ ] Add `capture_screenshot()` RPC method
- [ ] Test screenshot capture on Steam Deck (verify GStreamer + PipeWire work)
- [ ] Add "Test Capture" button in UI to verify

### Phase 4: OCR — Cloud Vision `[NOT STARTED]`
- [ ] Implement Cloud Vision OCR client in backend (async, with retry)
- [ ] Image size handling (resize if >10MB)
- [ ] Add `perform_ocr()` RPC method (capture + OCR pipeline)
- [ ] Display OCR text results in frontend
- [ ] Add "Test OCR" button that captures screen and shows detected text

### Phase 5: TTS — Cloud Text-to-Speech `[NOT STARTED]`
- [ ] Implement Cloud TTS client in backend (async, with retry)
- [ ] Voice selection support (multiple voices/languages)
- [ ] Speech rate presets (x-slow to x-fast)
- [ ] Audio playback via mpv/ffplay
- [ ] Add `perform_tts(text)` and `stop_playback()` RPC methods
- [ ] Settings UI — voice picker dropdown, speech rate selector, volume slider
- [ ] "Test TTS" button with text input field

### Phase 6: End-to-End OCR+TTS Pipeline `[NOT STARTED]`
- [ ] Wire capture → OCR → TTS into a single `read_screen()` RPC method
- [ ] Add "Read Screen" button in main UI
- [ ] Loading/progress states in UI during pipeline execution
- [ ] Error handling and user feedback for each pipeline stage
- [ ] Stop button to interrupt audio playback

### Phase 7: L4 Button Trigger `[NOT STARTED]`
- [ ] Implement hidraw-based button monitoring in Python backend (background thread)
- [ ] L4 press triggers `read_screen()` pipeline without opening UI
- [ ] Settings UI — button selection (L4/R4/L5/R5)
- [ ] Hold-time detection with configurable threshold
- [ ] Health monitoring and auto-reconnect

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
- **Debug Mode** (the `debug` setting toggle in the UI): when enabled, the backend should emit verbose/detailed logs using `decky.logger.debug()`. These are normally hidden by Decky Loader's log level but appear when Decky is in developer mode. Use debug-level logs for: RPC call parameters, settings reads/writes, directory listings, timing info, internal state. Use info-level logs for: lifecycle events, credential load/clear, errors.

---

## Key Technical Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Architecture | Single Decky plugin (no separate service) | Simpler deployment and lifecycle management |
| Screen capture | GStreamer + PipeWire | Native to Steam Deck, hardware-accelerated |
| Audio playback | mpv (preferred) or ffplay | Available on Steam Deck, supports MP3 |
| GCP credentials | Base64-encoded service account JSON | Simple storage, same pattern as reference plugin |
| Button input | Hidraw direct device reading | Works in background without opening UI |
| Settings storage | JSON file in DECKY_PLUGIN_SETTINGS_DIR | Standard Decky convention |
| Python deps | Bundled in py_modules/ via Docker build | Runs on Steam Deck without internet |

## File Structure

```
decky-cloud-reader/
├── src/
│   └── index.tsx              # Plugin entry, all UI (sections, file browser)
├── main.py                    # Python backend (all logic integrated)
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
