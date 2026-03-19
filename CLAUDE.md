# Decky Cloud Reader Plugin

## Project Overview

**Decky Loader plugin** for Steam Deck — OCR and TTS for text-heavy games. Two provider modes: **local** (RapidOCR + Piper TTS, offline, default) and **GCP** (Cloud Vision + Cloud TTS, online, requires service account).

## Development Environment

- **Host:** M1 MacBook Pro (ARM / Apple Silicon)
- **Target:** Steam Deck at `192.168.50.58` (SSH, passwordless sudo)
- **Build:** x86 Docker image with Python 3.13 (matching Steam Deck) → deploy via SSH

## Development Workflow

- Work in **small incremental steps**, test each change on Steam Deck immediately
- Build in x86 Docker container first, deploy via SSH
- **Comment code extensively** — treat me as someone unfamiliar with the structures and languages
- **Challenge vague requests** — question decisions, propose alternatives, ask clarifying questions

## Reference Projects

| Project | Path | Use For |
|---------|------|---------|
| Decky Plugin Template | `/Users/mshabalov/Documents/claude-projects/decky-plugin-template` | Scaffolding, build system, conventions |
| Decky-Translator | `/Users/mshabalov/Documents/claude-projects/Decky-Translator` | L4 button trigger, RapidOCR pattern (never for TTS) |
| decky-ocr-tts-claude-service-plugin | `/Users/mshabalov/Documents/claude-projects/decky-ocr-tts-claude-service-plugin` | UI features, OCR/TTS logic (uses separate service — NOT our architecture) |

---

## Architecture

Everything runs inside the standard Decky plugin process. Python backend (`main.py`) handles screen capture, dual worker management, provider routing, and audio playback. OCR/TTS is delegated to two **persistent subprocess workers** communicating via stdin/stdout JSON lines:
- `gcp_worker.py` — Google Cloud APIs, system Python 3.13, warm gRPC connections
- `local_worker.py` — RapidOCR + Piper TTS, bundled Python 3.12, pre-loaded ONNX models

```
Frontend (TypeScript/React)           Backend (Python)
┌──────────────────────────┐         ┌─────────────────────────────────┐
│ Decky Panel UI           │   RPC   │ main.py (Plugin class)          │
│  - Enabled toggle        │◄───────►│  - Pipeline orchestration       │
│  - Provider selection    │         │  - Provider routing (GCP/local) │
│  - Settings / credentials│         │  - Screen capture (ximagesrc)   │
│  - Capture config        │         │  - Dual worker lifecycle mgmt   │
│  - Translation config    │         │  - Audio playback (Popen)       │
│  - Version footer        │         │                                 │
│                          │         │  hidraw_monitor.py (thread)     │
│ Global Overlay (Phase 13) │         │  touchscreen_monitor.py (thread)│
│  - Region preview overlay│         │                                 │
└──────────────────────────┘         │  gcp_worker.py (persistent)     │
                                     │  local_worker.py (persistent)   │
                                     └─────────────────────────────────┘
```

---

## Implementation Progress

See @IMPLEMENTATION_HISTORY.md for completed phases (1–32).

---

## Critical Pitfalls & Lessons Learned

Auto-loaded from `.claude/rules/pitfalls.md` — covers RapidOCR, Piper TTS, subprocess env, Gamescope, Steam UI, touch handling, Decky sandbox, and more.

---

## Performance (Local Mode, Steam Deck)

| Metric | Cold Worker | Warm Worker |
|--------|-------------|-------------|
| Total pipeline | ~5.7s | ~2.1s |
| Worker startup | ~3s | 0s |
| OCR (RapidOCR) | ~1.4s | ~1.2s |
| TTS (Piper) | ~1.3s | ~0.7s |

Plugin zip: ~170 MB. OCR rec models: 8-85 MB each, downloaded on demand. Voices: ~63 MB each, downloaded on demand.

---

## Settings Reference

Auto-loaded from `.claude/rules/settings.md` — 24 settings with defaults and descriptions.

---

## Logging Conventions

- All backend logs use `[DCR]` prefix: `decky.logger.info(f"{LOG} message")`
- Filter on Deck: `journalctl -u plugin_loader -f | grep DCR`
- Debug mode: `decky.logger.setLevel(logging.DEBUG)` — synced at startup and on toggle change
- Debug level: RPC params, settings, timing, internal state. Info level: lifecycle, credentials, errors

---

## Key Technical Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Architecture | Single plugin + persistent subprocess workers | No separate service; workers stay warm for fast repeat calls |
| Dual workers | GCP (system Python 3.13) + local (bundled Python 3.12) | `rapidocr-onnxruntime` requires Python <3.13 |
| Worker protocol | stdin/stdout JSON lines | Simple, no network deps, supports serve + one-shot modes |
| Screen capture | GStreamer ximagesrc (primary) + pipewiresrc (fallback) | ximagesrc captures game window from Xwayland :1 (no Steam overlay); falls back to pipewiresrc composited output when no game is focused |
| Audio playback | ffplay (primary) / mpv / pw-play | Auto-discovered; needs `XDG_RUNTIME_DIR=/run/user/1000` (Decky runs as root); reaper thread prevents zombies |
| Button input | Hidraw direct reading | Background operation, no UI needed |
| Touchscreen | Raw evdev + `struct.unpack` | Stdlib only; ioctl axis calibration; 90° CW coordinate transform; auto-managed by touch input toggle |
| Capture controls | Independent toggle + button | Touch input (on/off + swipe/two-tap style) and trigger button (fixed region) work independently; touchscreen auto-started/stopped by toggle; PIL crop before OCR; during playback all touches = stop only |
| Pipeline optimization | Combined `ocr_tts` action for same-provider | Saves one round-trip; mixed providers or translation/filtering active → split |
| Translation | Free Google Translate | Unofficial `translate.googleapis.com` endpoint via curl subprocess — no credentials needed, works out of the box with just internet |
| Pipeline cancellation | `threading.Event` between steps | Simple; worker timeout bounded at 60s |
| Voice distribution | On-demand HuggingFace download | 16 voices / 14 language variants; persists in settings dir across updates; no zip bloat |
| OCR language models | On-demand HuggingFace download (monkt/paddleocr-onnx) | 7 language packs; rec models persist in settings dir; det/cls are universal+bundled; lazy engine init with single-engine cache |
| Default provider | Local (offline) | Works out of the box; GCP requires service account |
| Keyboard suppression | Frontend event-driven via `VirtualKeyboardManager` | No polling; callback fires on open/close; RPC notifies backend to guard touch handlers |
| Touch suppression | Three-flag guard: keyboard + modal + QAM | `useQuickAccessVisible()` for QAM; `useEffect` + RPC for modal; explicit reset on unmount |
| Text input in QAM | Full-screen modal via `showModal()` | `TextField` in QAM panel doesn't receive keyboard focus; modal gives proper focus + no keyboard overlap |
| Pipeline trigger | Hardware only (button/touchscreen) | No UI trigger button — pipeline runs exclusively via L4/R4/L5/R5 hold or touchscreen tap/swipe |
| Backend→frontend events | `decky.emit()` + `addEventListener` from `@decky/api` | Prefer event-driven push over polling for all backend→frontend notifications; zero overhead when idle |
| Versioning | `package.json` version + `@rollup/plugin-replace` | Build-time injection of `__PLUGIN_VERSION__`; single source of truth; version footer in panel |
| Docker build | Layer caching enabled | Use `--no-cache` when requirements or model URLs change |

---

## File Structure

```
decky-cloud-reader/
├── src/index.tsx              # All UI (sections, file browser, provider selection)
├── main.py                    # Backend (lifecycle, RPC, pipeline, dual worker mgmt)
├── hidraw_monitor.py          # Button hold detection (hidraw, background thread)
├── touchscreen_monitor.py     # Touch detection (evdev, background thread, down/up/tap callbacks)
├── gcp_worker.py              # GCP worker (persistent/one-shot, system Python 3.13)
├── local_worker.py            # Local worker (persistent/one-shot, bundled Python 3.12)
├── requirements.txt           # GCP deps (Python 3.13)
├── requirements_local.txt     # Local inference deps (Python 3.12)
├── package.json / plugin.json / tsconfig.json / rollup.config.js
├── audio/                     # Sound effect WAV files (Phase 11)
├── docker/Dockerfile.plugin + docker-compose.yml
├── deploy.sh
├── CLAUDE.md
└── GOOGLE_CLOUD_SETUP.md   # GCP service account setup guide
```

**Built by Docker (deployed to Deck):**
- `py_modules/` — GCP packages (cpython-313-x86_64)
- `py_modules_local/` — Local inference packages (cpython-312-x86_64)
- `python312/python/bin/python3.12` — Bundled interpreter
- `models/ocr/` — PP-OCRv5 det.onnx (universal) + v2 cls model (rec models downloaded per-language on demand)

**Downloaded on demand:**
- `DECKY_PLUGIN_SETTINGS_DIR/ocr_models/{language_id}/rec.onnx` + `dict.txt` (8-85 MB per language)
- `DECKY_PLUGIN_SETTINGS_DIR/voices/*.onnx` (~63 MB each)
