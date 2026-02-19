# Decky Cloud Reader Plugin

## Project Overview

**Decky Loader plugin** for Steam Deck — OCR and TTS for text-heavy games. Two provider modes: **local** (RapidOCR + Piper TTS, offline, default) and **GCP** (Cloud Vision + Cloud TTS, online, requires service account).

## Development Environment

- **Host:** M1 MacBook Pro (ARM / Apple Silicon)
- **Target:** Steam Deck at `192.168.50.116` (SSH, passwordless sudo)
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
│  - Read Screen (primary) │◄───────►│  - Pipeline orchestration       │
│  - Provider selection    │         │  - Provider routing (GCP/local) │
│  - Settings / credentials│         │  - Screen capture (GStreamer)   │
│  - Button trigger config │         │  - Dual worker lifecycle mgmt   │
│  - Capture mode config   │         │  - Audio playback (Popen)       │
│  - Enabled toggle gates  │         │                                 │
│                          │         │  hidraw_monitor.py (thread)     │
│ Global Overlay            │         │  touchscreen_monitor.py (thread)│
│  [Phase 13 — planned]    │         │                                 │
└──────────────────────────┘         │  gcp_worker.py (persistent)     │
                                     │  local_worker.py (persistent)   │
                                     └─────────────────────────────────┘
```

---

## Implementation Progress

### Completed Phases (1–12)

| Phase | Summary |
|-------|---------|
| **1: Foundation** | Plugin scaffolding, Docker build, SSH deploy, RPC communication |
| **2: Settings** | Settings manager, GCP credential storage (base64), file browser UI, validation |
| **3: Screen Capture** | GStreamer + PipeWire screenshot capture |
| **4: GCP OCR** | `gcp_worker.py` with Cloud Vision, image resize, retry logic |
| **5: GCP TTS** | Cloud TTS in `gcp_worker.py`, audio playback (ffplay/mpv/pw-play auto-discovery), daemon reaper thread |
| **6: Pipeline** | End-to-end `read_screen()` → OCR → TTS → playback, cancellation via `threading.Event` |
| **6.5: Persistent GCP Worker** | Converted one-shot subprocess to persistent stdin/stdout JSON worker; warm gRPC saves ~1.5s/call |
| **7: L4 Button** | `hidraw_monitor.py` — hidraw device reading, hold detection, auto-reconnect, configurable button/threshold |
| **7.5: Enabled Toggle** | Master switch stops workers + playback + pipeline; hidraw monitor stays running (cheap) |
| **8: Local OCR/TTS** | `local_worker.py` with RapidOCR + Piper TTS, bundled Python 3.12, dual worker routing |
| **8.5: Multiple Voices** | Lazy-load voice caching, 4 bundled English voices |
| **8.6: On-Demand Voices** | No bundled voices; 16 curated voices (14 language variants) downloaded from HuggingFace on demand to `DECKY_PLUGIN_SETTINGS_DIR/voices/` |
| **9: Touchscreen** | `touchscreen_monitor.py` — evdev tap detection, axis calibration via ioctl, 90° coordinate transform |
| **10: Settings Defaults** | Added config fields for capture modes, regions, text filtering, and mute toggle |
| **11: Sound Effects** | Fire-and-forget `_play_interface_sound()` independent of TTS, mute toggle, 3 test buttons, Dockerfile audio/ copy |
| **12: Capture Modes** | 5 capture modes (full_screen, swipe_selection, two_tap_selection, fixed_region, hybrid), touchscreen auto-management, PIL image cropping in workers, state machine for two-tap/swipe, mode-aware UI |

### Phase 13: Global Overlay `[NOT STARTED]`
- [ ] Visual overlay for displaying selected capture zones on screen, reference: `/Users/mshabalov/Documents/claude-projects/Decky-Translator`

### Phase 14: Text Filtering `[NOT STARTED]`
- [ ] Backend word filtering in `main.py` (after OCR, before TTS) — parse comma-separated ignore lists, respect enabled toggles
- [ ] Frontend settings section for `ignored_words_always`, `ignored_words_always_enabled`, `ignored_words_beginning`, `ignored_words_beginning_enabled`, `ignored_words_count`

---

## Critical Pitfalls & Lessons Learned

### RapidOCR
- Use `rapidocr-onnxruntime` (NOT `rapidocr` v3.x) — lighter deps, proven API, same as Decky-Translator
- **Never pass `rec_keys_path`** — causes `IndexError` due to model-dictionary mismatch. Pass custom ONNX model paths only (`det_model_path`, `rec_model_path`, `cls_model_path`); library's built-in keys match
- Result format: `result = engine(img)` → `result[0]` is list of `[bbox, text, confidence]` or `None`. Do NOT tuple-unpack

### Piper TTS (>=1.4.0)
- Use `synthesize_wav(text, wav_file, syn_config=SynthesisConfig(length_scale=..., speaker_id=...))` — NOT `synthesize()`
- `speaker_id` goes in `SynthesisConfig`, NOT as a method argument
- Non-English voices can only phonemize their own script — English text produces garbled output (expected)

### Voice HuggingFace URLs
- Pattern: `{base}/{lang_family}/{lang_code}/{speaker}/{quality}/{voice_id}.onnx`
- Some voices don't follow obvious naming (e.g., Ukrainian is `uk_UA-ukrainian_tts-medium`). Always verify against the actual repo tree

### Subprocess Environment
- **Strip `LD_LIBRARY_PATH` and `LD_PRELOAD`** when spawning system commands (curl, etc.) — Decky Loader (PyInstaller) bundles older libssl.so.3 that breaks system binaries

### Decky Plugin Sandbox
- `sys.path` doesn't include plugin dir — must add manually before importing split-out `.py` files
- Dockerfile Stage 4 copies specific files — new `.py` files must be added explicitly

---

## Performance (Local Mode, Steam Deck)

| Metric | Cold Worker | Warm Worker |
|--------|-------------|-------------|
| Total pipeline | ~5.7s | ~2.1s |
| Worker startup | ~3s | 0s |
| OCR (RapidOCR) | ~1.4s | ~1.2s |
| TTS (Piper) | ~1.3s | ~0.7s |

Plugin zip: ~241 MB. Voices: ~63 MB each, downloaded on demand.

---

## Settings Reference

| Setting | Default | Description |
|---------|---------|-------------|
| `enabled` | `true` | Master switch — stops workers, playback, pipeline when disabled |
| `debug` | `false` | Enables `DEBUG` log level (no restart needed) |
| `ocr_provider` | `"local"` | `"gcp"` or `"local"` |
| `tts_provider` | `"local"` | `"gcp"` or `"local"` |
| `voice_id` | `"en-US-Neural2-C"` | GCP Neural2 voice |
| `speech_rate` | `"medium"` | GCP speech rate |
| `local_voice_id` | `"en_US-amy-medium"` | Piper voice (auto-downloads on first use) |
| `local_speech_rate` | `"medium"` | Piper speech rate |
| `volume` | `100` | TTS volume 0-100 |
| `trigger_button` | `"L4"` | Hidraw button: disabled/L4/R4/L5/R5 |
| `hold_time_ms` | `500` | Button hold threshold |
| `capture_mode` | `"full_screen"` | Capture method: full_screen, swipe_selection, two_tap_selection, fixed_region, hybrid |
| `mute_interface_sounds` | `false` | Disable/enable playback of UI feedback sounds |
| `fixed_region_x1` | `0` | Fixed region left X coordinate |
| `fixed_region_y1` | `0` | Fixed region top Y coordinate |
| `fixed_region_x2` | `1280` | Fixed region right X coordinate |
| `fixed_region_y2` | `800` | Fixed region bottom Y coordinate |
| `last_selection_x1` | `0` | Last selection left X (auto-saved) |
| `last_selection_y1` | `0` | Last selection top Y (auto-saved) |
| `last_selection_x2` | `1280` | Last selection right X (auto-saved) |
| `last_selection_y2` | `800` | Last selection bottom Y (auto-saved) |
| `ignored_words_always` | `""` | Comma-separated words to remove anywhere in text |
| `ignored_words_always_enabled` | `false` | Enable/disable the "always ignore" list |
| `ignored_words_beginning` | `""` | Comma-separated words to remove from start of text |
| `ignored_words_beginning_enabled` | `false` | Enable/disable the "ignore at beginning" list |
| `ignored_words_count` | `3` | How many leading words to check for "beginning" list |

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
| Screen capture | GStreamer + PipeWire | Native to Steam Deck |
| Audio playback | ffplay (primary) / mpv / pw-play | Auto-discovered; needs `XDG_RUNTIME_DIR=/run/user/1000` (Decky runs as root); reaper thread prevents zombies |
| Button input | Hidraw direct reading | Background operation, no UI needed |
| Touchscreen | Raw evdev + `struct.unpack` | Stdlib only; ioctl axis calibration; 90° CW coordinate transform; auto-managed by capture mode |
| Capture modes | State machine in main.py | 5 modes; touchscreen auto-started/stopped per mode; PIL crop before OCR; during playback all touches = stop only |
| Pipeline optimization | Combined `ocr_tts` action for same-provider | Saves one round-trip; mixed providers run sequentially |
| Pipeline cancellation | `threading.Event` between steps | Simple; worker timeout bounded at 60s |
| Voice distribution | On-demand HuggingFace download | 16 voices / 14 language variants; persists in settings dir across updates; no zip bloat |
| Default provider | Local (offline) | Works out of the box; GCP requires service account |
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
└── CLAUDE.md
```

**Built by Docker (deployed to Deck):**
- `py_modules/` — GCP packages (cpython-313-x86_64)
- `py_modules_local/` — Local inference packages (cpython-312-x86_64)
- `python312/python/bin/python3.12` — Bundled interpreter
- `models/ocr/` — PaddleOCR v4 ONNX models (NO `ppocr_keys_v1.txt`)

**Downloaded on demand:** `DECKY_PLUGIN_SETTINGS_DIR/voices/*.onnx` (~63 MB each)
