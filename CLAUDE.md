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

### Completed Phases (1–32)

| Phase | Summary |
|-------|---------|
| **1: Foundation** | Plugin scaffolding, Docker build, SSH deploy, RPC communication |
| **2: Settings** | Settings manager, GCP credential storage (base64), file browser UI, validation |
| **3: Screen Capture** | GStreamer ximagesrc (game window via Xwayland :1, overlay-free) with pipewiresrc fallback |
| **4: GCP OCR** | `gcp_worker.py` with Cloud Vision, image resize, retry logic |
| **5: GCP TTS** | Cloud TTS in `gcp_worker.py`, audio playback (ffplay/mpv/pw-play auto-discovery), daemon reaper thread |
| **6: Pipeline** | End-to-end `read_screen()` → OCR → TTS → playback, cancellation via `threading.Event` |
| **6.5: Persistent GCP Worker** | Converted one-shot subprocess to persistent stdin/stdout JSON worker; warm gRPC saves ~1.5s/call |
| **7: L4 Button** | `hidraw_monitor.py` — hidraw device reading, hold detection, auto-reconnect, configurable button/threshold |
| **7.5: Enabled Toggle** | Master switch stops workers + playback + pipeline; hidraw monitor stays running (cheap) |
| **8: Local OCR/TTS** | `local_worker.py` with RapidOCR + Piper TTS, bundled Python 3.12, dual worker routing |
| **8.5: Multiple Voices** | Lazy-load voice caching, 4 bundled English voices |
| **8.6: On-Demand Voices** | No bundled voices; 16 curated voices (14 language variants) downloaded from HuggingFace on demand to `DECKY_PLUGIN_SETTINGS_DIR/voices/` |
| **9: Touchscreen** | `touchscreen_monitor.py` — evdev tap detection, axis calibration via ioctl, 90° coordinate transform; frontend fetches status on mount only (no polling) |
| **10: Settings Defaults** | Added config fields for capture modes, regions, text filtering, and mute toggle |
| **11: Sound Effects** | Fire-and-forget `_play_interface_sound()` independent of TTS, mute toggle, Dockerfile audio/ copy |
| **12: Capture Modes** | Touchscreen auto-management, PIL image cropping in workers, state machine for two-tap/swipe, debounced region slider saves (800ms). *(Originally 5 mutually exclusive modes — simplified to independent controls in Phase 29)* |
| **13: Global Overlay** | Region preview overlay: `capture_overlay_screenshot()` RPC, `OverlayState` class, `RegionPreviewOverlay` global component mounted/unmounted on demand via `routerHook`, `useUIComposition` for Gamescope layer, auto-close on QAM dismiss/tab switch + 10s auto-dismiss timeout, spotlight cutout for fixed region |
| **13.5: Keyboard Suppression** | Event-driven on-screen keyboard detection via Steam's `VirtualKeyboardManager` (`m_bIsInlineVirtualKeyboardOpen` observable). Frontend registers callback in `definePlugin()`, calls `set_keyboard_visible()` RPC on open/close. Backend `_keyboard_visible` flag guards all touch callbacks — suppresses two-tap/swipe gestures while typing |
| **14: Text Filtering** | `_apply_text_filters()` in `main.py` — two modes: "always" (whole-word case-insensitive removal anywhere) and "beginning" (remove from first N tokens, punctuation-tolerant). Pipeline forces separate OCR→filter→TTS when filtering active (skips combined `ocr_tts`). Frontend section with toggles, `WordFilterModal` (full-screen modal via `showModal()` for proper keyboard focus), and word-count slider with live value label |
| **14.5: Touch Suppression** | Three-flag touch suppression: `_keyboard_visible` (Phase 13.5), `_modal_visible` (modal dialogs), `_qam_visible` (QAM "..." menu via `useQuickAccessVisible()` hook from `@decky/ui`). Each has a frontend→backend RPC (`set_keyboard_visible`, `set_modal_visible`, `set_qam_visible`). All three touch callbacks (`_on_touch_down/up/tap`) check all flags. QAM flag reset on Content unmount to prevent stuck suppression |
| **15: Remove Read Text Button** | Removed "Read Text" / "Stop Playback" test button from TTS section, playback polling, and all related state/handlers. Pipeline trigger via button/touchscreen is the only flow |
| **16: Remove Sound Effect Test Buttons** | Removed 3 sound test buttons (Test Start/End/Stop Sound) from Sound Effects section. Kept Mute Interface Sounds toggle only |
| **17: Remove Test Capture & Test OCR** | Removed standalone "Test Capture" button (Screen Capture section) and "Test OCR" button (OCR section) with status messages and OCR text display. Removed the entire Screen Capture and OCR sections from UI |
| **18: Remove Read Screen Section** | Removed "Read Screen" / "Stop" button, pipeline progress indicator, pipeline polling, `getPipelineStepLabel()` helper, and all pipeline/playback RPC callables. Replaced top section with "Cloud Reader" containing just the Enabled toggle. Moved Debug Mode toggle to its own "Debug" section |
| **19: Versioning** | Version `1.0.0` in `package.json` as single source of truth. `@rollup/plugin-replace` injects `__PLUGIN_VERSION__` at build time via `rollup.config.js`. Version footer at bottom of plugin panel ("Plugin v1.0.0") |
| **20: GCP Voice Expansion** | Expanded GCP voice dropdown from 8 English-only to 28 multi-language voices (EN-US, EN-GB, UK, DE, FR, ES, JA, PT-BR, RU). Includes Neural2, Wavenet, and Standard voices. Updated `VOICE_OPTIONS` in frontend and `VOICE_REGISTRY` in `gcp_worker.py`. Adopted reference plugin label format |
| **21: Debug-Only Monitor Status** | Moved button monitor and touchscreen status indicators from their respective sections into the Debug section. Both only render when Debug Mode is ON, reducing UI clutter for normal use. `scrollIntoView` on a ref + invisible `Focusable` spacer to fix QAM scroll container not recalculating height after dynamic content appears |
| **22: Zero Hold Time Option** | Added "Instant (0ms)" option to the hold time dropdown. Backend's `>=` comparison handles 0 naturally — trigger fires immediately on press. Hint text adapts: "Press L4 to trigger" instead of "Hold L4 for 0ms" |
| **23: Pipeline Feedback** | Three feedback mechanisms for pipeline results: (A) "no_text" sound effect plays on failure/no-text (respects mute); "stop" sound plays at 50% volume, (B) on-screen toast overlay via event-driven `decky.emit("pipeline_toast")` → `addEventListener` — shows "Reading...", "N words read" (green, 3s), "No text found" (yellow, 4s), "Error" (red, 4s), auto-dismisses cancelled immediately; `PipelineToast` child uses `useUIComposition(Notification)` only while visible; `hide_pipeline_toast` setting disables toast display, (C) "Last Pipeline" debug indicator in Debug section shows last result color-coded |
| **24: Dead Code Cleanup** | Removed 3 unused backend RPC methods: `get_pipeline_status()` (Phase 18 removed UI), `get_playback_status()` (Phase 15 removed UI), `get_last_touch()` (never wired to frontend). Also cleaned stale comment referencing `get_playback_status()` polling |
| **25: Multi-Language OCR** | 7 OCR language packs (English, Chinese/Japanese, Korean, Latin, Cyrillic, Thai, Greek) with on-demand rec model downloads from HuggingFace (`monkt/paddleocr-onnx`). Det/cls use rapidocr-onnxruntime's built-in models (v5 det was incompatible — over-segmentation). Recognition models downloaded per-language to `DECKY_PLUGIN_SETTINGS_DIR/ocr_models/{language_id}/`. Lazy OCR engine init in local worker (one cached engine, reinit on language change). GCP Vision API receives `language_hints` based on `ocr_language` setting when available, otherwise auto-detects. Frontend: language dropdown in Provider section with download/delete controls. Plugin zip ~85 MB smaller (removed all bundled OCR models) |
| **26: Translation Pipeline** | Optional GCP Cloud Translation between OCR and TTS for playing games in foreign languages (e.g., JA game → OCR → Translate JA→EN → TTS English). Uses Translation API v3 (gRPC transport — immune to PyInstaller SSL contamination). Lazy-initialized `TranslationServiceClient` cached in `gcp_worker.py`. Pipeline: capture → OCR → translate → filter → TTS (always splits, no combined `ocr_tts` when translation active). Source language auto-derived from `ocr_language` setting via `TRANSLATION_SOURCE_LANGUAGE` mapping (`None` = auto-detect for multi-language OCR groups). 15 target languages. UI: Translation section between Provider and GCP Credentials (visible only when `needsGcp`). GCP worker env now strips `LD_LIBRARY_PATH`/`LD_PRELOAD` to fix OAuth2 token refresh for lazy-initialized clients |
| **27: Spoken Text Overlay** | Optional text overlay replaces "N words read" pill toast with actual spoken text + scanned region border. `show_text_overlay` setting (off by default, hidden when `hide_pipeline_toast` is on). `SpokenTextOverlay` component: text top-left aligned inside region box (`boxSizing: "border-box"`, 4px padding, `rgba(0,0,0,0.99)` near-opaque background, cyan border) with dynamic font sizing (`FIT_FACTOR=2.0`, min 7px, max 16px for regions). Full-screen: centered subtitle bar at bottom. Backend emits 3 new args on `pipeline_toast` event: `text`, `crop_region`, `show_overlay`. Text truncated at 500 chars. Overlay stays visible for entire TTS playback duration via `pipeline_toast_dismiss` event emitted by reaper thread on natural audio finish (exit code 0); 45s safety-net timeout. Cancelled toast shows "Stopped" pill for 1.5s. Trigger cooldown (`TRIGGER_COOLDOWN_S=0.8`) prevents accidental double-taps in all handlers (button + touch_down/up/tap) |
| **28: Pipeline Hardening** | Two reliability fixes: (1) Two-tap minimum crop region (50x50 pixels) — mirrors existing swipe check; tapping twice close together plays stop sound and discards instead of wasting OCR/API quota. Applies to `two_tap` and `swipe` touch input styles. (2) Stderr drain thread liveness check — `_drain_worker_stderr()` and `_drain_local_worker_stderr()` now use `readline()` + `poll()` loop instead of bare `for line in stderr:`, so threads exit promptly if the worker process dies without cleanly closing its pipe |
| **29: Mode-Free Capture** | Replaced 5 mutually exclusive capture modes with two independent controls: **Touch Input toggle** (on/off + swipe/two-tap style selector) and **Trigger Button dropdown** (None/L4-R5 for fixed-region capture). Button always captures fixed region (default 0,0,1280,800 = full screen). Touch and button work independently — both can be active simultaneously. Removed `capture_mode` and `touchscreen_enabled` settings; added `touch_input_enabled` and `touch_input_style`. UI: single "Capture" section replaces old "Button Trigger" + "Capture Mode" sections. Trigger button label changed from "Disabled" to "None". Deploy script now deletes settings.json for clean state |
| **30: Conditional OCR Language** | OCR Language dropdown + download/delete controls only visible when local OCR engine selected; moved directly below OCR Engine selector for logical grouping. GCP Vision relies on `language_hints` from saved `ocr_language` setting (defaults to English) with auto-detection fallback |
| **31: GCP Credentials Before Translation** | Swapped GCP Credentials and Translation section order — credentials now appear directly below Provider section (right after the "not configured" hint), translation follows after |
| **32: Free Google Translate** | Added free translation provider using unofficial `translate.googleapis.com/translate_a/single?client=gtx` endpoint — no GCP credentials needed. New `translation_provider` setting (`"free"` default, `"gcp"` optional). Free translation implemented as curl subprocess in `main.py` with stripped `LD_LIBRARY_PATH` (PyInstaller SSL fix). Translation section now always visible (not gated by `needsGcp`). `needsGcp` updated to include GCP translation provider. Pre-flight credential checks updated for GCP translation edge case. UI: Translation Engine dropdown between Enable toggle and Translate To dropdown, red hint when GCP selected without credentials |

---

## Critical Pitfalls & Lessons Learned

### RapidOCR
- Use `rapidocr-onnxruntime` (NOT `rapidocr` v3.x) — lighter deps, proven API, same as Decky-Translator
- **`rec_keys_path` handling**: For the old bundled v4 Chinese models, do NOT pass `rec_keys_path` (causes `IndexError` from model-dictionary mismatch). For downloaded PP-OCRv5 language models, you MUST pass `rec_keys_path=dict.txt` because each language has its own character dictionary that matches its rec model
- Result format: `result = engine(img)` → `result[0]` is list of `[bbox, text, confidence]` or `None`. Do NOT tuple-unpack

### Piper TTS (>=1.4.0)
- Use `synthesize_wav(text, wav_file, syn_config=SynthesisConfig(length_scale=..., speaker_id=...))` — NOT `synthesize()`
- `speaker_id` goes in `SynthesisConfig`, NOT as a method argument
- Non-English voices can only phonemize their own script — English text produces garbled output (expected)

### Voice HuggingFace URLs
- Pattern: `{base}/{lang_family}/{lang_code}/{speaker}/{quality}/{voice_id}.onnx`
- Some voices don't follow obvious naming (e.g., Ukrainian is `uk_UA-ukrainian_tts-medium`). Always verify against the actual repo tree

### Subprocess Environment
- **Strip `LD_LIBRARY_PATH` and `LD_PRELOAD`** when spawning subprocesses (GCP worker, curl, etc.) — Decky Loader (PyInstaller) bundles older libssl.so.3 that breaks Python's `ssl` module for `requests`/`urllib3`. This affects OAuth2 token refresh for lazy-initialized GCP clients (Translation) even though the actual API calls use gRPC (bundled BoringSSL, unaffected). Vision/TTS dodge this because gRPC handles their auth internally via its C core, but new clients trigger a fresh token refresh through the Python `requests` path

### Gamescope Screen Capture
- Gamescope runs two Xwayland displays: `:0` (Steam UI/overlay) and `:1` (game windows)
- **ximagesrc** on `:1` with a specific window XID captures game-only content (no Steam overlay)
- Game window XID is read from `GAMESCOPE_FOCUSED_WINDOW` X atom on `:0` root via `xprop`
- **pipewiresrc** with `XDG_SESSION_TYPE=wayland` captures the composited output (game + overlay)
- When no game is focused (home screen), `GAMESCOPE_FOCUSED_WINDOW` returns a Steam UI window on `:0` which causes `BadWindow` on `:1` — must fall back to pipewiresrc
- Avoid `XDG_SESSION_TYPE=x11` hack with pipewiresrc — it works today but is fragile as SteamOS moves to Wayland

### Steam VirtualKeyboardManager
- Accessed via `findModuleChild` → module with `m_WindowStore` → `ActiveWindowInstance.VirtualKeyboardManager` (getter on prototype)
- The observable property is **`m_bIsInlineVirtualKeyboardOpen`** (NOT `m_bIsVirtualKeyboardOpen` as in Decky-OSKPlus — that property no longer exists on current SteamOS)
- Register callback: `vkm.m_bIsInlineVirtualKeyboardOpen.m_callbacks.Register(cb)` — returns `{Unregister()}` handle
- Callback receives a boolean: `true` when keyboard opens, `false` when it closes

### Touch handler callback ordering race
A single physical touch fires three callbacks sequentially on the event loop: `_handle_touch_down` → `_handle_touch_up` → `_handle_touch_tap`. The `_touch_started_during_playback` flag must survive all three — **only clear it at the start of the next `_handle_touch_down`**, never in `_handle_touch_up`. In `_handle_touch_tap`, check the flag **before** checking `_is_playing`/`_pipeline_running`, because `_handle_touch_down` may have already stopped playback (clearing `_is_playing`) but `_pipeline_running` may still be True, causing a duplicate `_stop_and_sound()` call (double stop sound).

### QAM visibility and component unmount
`useQuickAccessVisible()` (from `@decky/ui`) detects QAM open/close via `visibilitychange` on the QuickAccess window. However, when the QAM closes, the `Content` component unmounts before the `useEffect` for `isQamVisible=false` can fire, leaving `_qam_visible` stuck at `true`. **Always reset QAM flag in the unmount cleanup** (`setQamVisible(false)` in the `useEffect([], [])` cleanup return).

### TextField in QAM panel
`TextField` from `@decky/ui` placed directly in the QAM panel does NOT receive proper keyboard focus — the on-screen keyboard appears but input goes to the wrong target, and the keyboard is partially covered by the panel. **Use `showModal()` with a `ModalRoot`** containing the `TextField` instead. The full-screen modal gets proper focus and the keyboard has room.

### QAM panel scroll with dynamic content
Steam's QAM scroll container does NOT recalculate its scrollable height when content is dynamically added/removed (e.g., a toggle revealing extra fields). `window.dispatchEvent(new Event("resize"))` does NOT work in the Steam CEF browser. **Two workarounds:**
1. **`scrollIntoView`** — place a `ref` at the bottom of the new content, call `ref.current?.scrollIntoView({ behavior: "smooth", block: "nearest" })` via `setTimeout(..., 100)` after the state change
2. **Invisible `Focusable` spacer** at the very bottom of the panel — ensures gamepad D-pad navigation can always reach the last item: `<Focusable style={{ height: "1px", opacity: 0 }} onActivate={() => {}} />`

### Backend→frontend events (prefer over polling)
Decky provides `decky.emit(event_name, *args)` (Python) and `addEventListener`/`removeEventListener` (TypeScript, from `@decky/api`). **Always prefer this event-driven push over `setInterval` polling** for backend→frontend notifications. Polling wastes RPC calls when idle and adds latency (up to the poll interval). Events fire instantly with zero idle overhead. Pattern:
- Backend: `await decky.emit("my_event", arg1, arg2)`
- Frontend: `const listener = addEventListener<[arg1: type, arg2: type]>("my_event", (a1, a2) => { ... })`
- Cleanup: `removeEventListener("my_event", listener)` in `onDismount()`

### ffplay exit codes
ffplay does NOT use standard POSIX signal exit codes. When killed via `proc.terminate()` (SIGTERM), ffplay exits with code **123** (not -15). Natural playback completion with `-autoexit` exits with **0**. Always check `proc.returncode == 0` for natural finish, not `>= 0`.

### CSS box-sizing for game coordinate overlays
When positioning overlay elements using game coordinates (vw/vh percentages), padding and border add to the element's visual size with the default `content-box` model. A 10px padding + 2px border makes the rectangle 24px wider and taller than intended. **Always use `boxSizing: "border-box"`** so padding and border are contained within the specified dimensions.

### Gamescope notification layer and backdrop-filter
`backdrop-filter: blur()` has **no effect** on Gamescope's notification composition layer — the game renders in a separate compositor layer, so CSS blur cannot reach through. Use opaque/near-opaque backgrounds instead for text readability.

### Trigger cooldown across all touch callbacks
A single physical touch fires 3 independent callbacks: `touch_down` → `touch_up` → `touch_tap`. Cooldown checks must be applied in **all three** handlers, not just `touch_down`. Without this, `touch_tap` fires after `touch_down` is blocked by cooldown and triggers unintended actions (e.g., double stop sound).

### Decky Plugin Sandbox
- **`plugin.json` must use `"flags": ["root"]`** (exact string `"root"`, NOT `"_root"`). Decky's `sandboxed_plugin.py` checks `"root" in self.flags` — list exact-match, not substring. With `"_root"`, the plugin silently drops to the `deck` user via `setuid`/`setgid` (without `initgroups`, so no supplementary groups). The `deck` user can open `/dev/hidraw*` (Valve udev `uaccess` rules) but NOT `/dev/input/event*` (`root:input 660`). Root is required for touchscreen evdev access.
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

Plugin zip: ~170 MB. OCR rec models: 8-85 MB each, downloaded on demand. Voices: ~63 MB each, downloaded on demand.

---

## Settings Reference

| Setting | Default | Description |
|---------|---------|-------------|
| `enabled` | `true` | Master switch — stops workers, playback, pipeline when disabled |
| `debug` | `false` | Enables `DEBUG` log level (no restart needed) |
| `ocr_provider` | `"local"` | `"gcp"` or `"local"` |
| `ocr_language` | `"english"` | OCR recognition language: english, chinese, korean, latin, eslav, thai, greek |
| `tts_provider` | `"local"` | `"gcp"` or `"local"` |
| `voice_id` | `"en-US-Neural2-C"` | GCP Neural2 voice |
| `speech_rate` | `"medium"` | GCP speech rate |
| `local_voice_id` | `"en_US-amy-medium"` | Piper voice (auto-downloads on first use) |
| `local_speech_rate` | `"medium"` | Piper speech rate |
| `volume` | `100` | TTS volume 0-100 |
| `trigger_button` | `"L4"` | Hidraw button: None/L4/R4/L5/R5 |
| `hold_time_ms` | `500` | Button hold threshold |
| `touch_input_enabled` | `false` | Enable touchscreen gestures (swipe or two-tap) for OCR region selection |
| `touch_input_style` | `"two_tap"` | Touch gesture style: `"swipe"` (drag to select) or `"two_tap"` (tap two corners) |
| `mute_interface_sounds` | `false` | Disable/enable playback of UI feedback sounds |
| `hide_pipeline_toast` | `false` | Hide on-screen toast overlay with pipeline status |
| `show_text_overlay` | `false` | Show spoken text + region border overlay instead of word count pill |
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
| `translation_enabled` | `false` | Enable translation between OCR and TTS |
| `translation_provider` | `"free"` | `"free"` (unofficial Google Translate, no credentials) or `"gcp"` (Cloud Translation v3, requires service account) |
| `translation_target_language` | `"en"` | ISO 639-1 target language code for translation |

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
| Translation | Free Google Translate (default) + GCP Cloud Translation v3 (optional) | Free: unofficial `translate.googleapis.com` endpoint via curl subprocess (no credentials, no worker dependency). GCP: v3 gRPC via gcp_worker (immune to PyInstaller SSL). Free is default — works out of the box with just internet |
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
