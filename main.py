# =============================================================================
# Decky Cloud Reader — Python Backend
# =============================================================================
#
# This is the backend of the Decky plugin. It runs as a Python process managed
# by the Decky Loader on the Steam Deck. The frontend (src/index.tsx) calls
# methods on this Plugin class via RPC using @decky/api's `callable()`.
#
# Lifecycle hooks (called automatically by Decky Loader):
#   _main()      — Called once when the plugin is loaded. Use for initialization.
#   _unload()    — Called when the plugin is stopped (but not removed).
#   _uninstall() — Called after _unload() when the plugin is fully removed.
#
# Regular methods (called from the frontend via `callable()`):
#   Any async method on the Plugin class can be called from TypeScript.
#   The method name in Python must match the string passed to `callable()`.
#
# The `decky` module is injected by Decky Loader at runtime — it provides
# logging, path constants, and event helpers. See decky.pyi for type stubs.
# =============================================================================

import os
import json
import base64
import logging
import glob
import traceback
import subprocess
import signal
import shutil
import tempfile
import asyncio
import time
import threading
from concurrent.futures import ThreadPoolExecutor

import decky

# Decky Loader runs main.py in a sandbox that doesn't add the plugin directory
# to sys.path. We need to add it manually so we can import hidraw_monitor.py
# which lives alongside main.py in the plugin directory.
import sys
_plugin_dir = os.path.dirname(os.path.realpath(__file__))
if _plugin_dir not in sys.path:
    sys.path.insert(0, _plugin_dir)

# Import the hidraw button monitor for hardware button trigger support.
# This module handles all low-level HID communication with the Steam Deck
# controller. It's in a separate file to keep main.py focused on plugin logic.
from hidraw_monitor import HidrawButtonMonitor, TRIGGER_BUTTONS

# Import the touchscreen monitor for capacitive touch input support (Phase 9).
# This module reads raw input events from /dev/input/eventN to detect taps
# on the Steam Deck's touchscreen.
from touchscreen_monitor import TouchscreenMonitor

# Log prefix — makes our messages easy to find in the Decky Loader journal.
# Usage: decky.logger.info(f"{LOG} message here")
# In the journal, lines will look like: "[DCR] backend loaded"
# Filter with: journalctl -u plugin_loader -f | grep DCR
LOG = "[DCR]"

# =============================================================================
# Interface sound effects — short WAV files for UI feedback (Phase 11)
# =============================================================================
# These sounds play independently of TTS playback (fire-and-forget) to provide
# audible feedback during capture mode interactions (e.g., selection start/end).
# Sound names are used as keys in _play_interface_sound() and the RPC method.
INTERFACE_SOUNDS = {
    "selection_start": "mixkit-modern-technology-select-3124.wav",
    "selection_end": "mixkit-old-camera-shutter-click-1137.wav",
    "stop": "mixkit-click-error-1110.wav",
}

# Timeout for the GStreamer capture subprocess. 2 seconds is sufficient —
# the pipeline typically completes well under this on Steam Deck.
CAPTURE_TIMEOUT = 2  # seconds

# Timeout for the OCR subprocess (gcp_worker.py). This covers:
#   - System Python startup and import time (~2-3s first run, cached after)
#   - Image resize if needed (~1-2s)
#   - Vision API call + network round trip (~5-15s)
#   - Up to 3 retries with backoff on transient errors (~3s extra)
# 45 seconds gives plenty of headroom even on a slow connection.
OCR_TIMEOUT = 45  # seconds

# Timeout for the TTS subprocess. TTS API is generally faster than Vision
# (smaller payloads, simpler processing). 30 seconds with retries.
TTS_TIMEOUT = 30  # seconds

# Timeout for the combined OCR+TTS subprocess. This runs both API calls
# sequentially in a single process, so the timeout needs to cover both.
# 60 seconds is generous — the combined call typically takes ~3s.
OCR_TTS_TIMEOUT = 60  # seconds

# Timeout for voice model downloads from HuggingFace. Each voice is ~63MB,
# so 120s should be plenty even on slow connections.
VOICE_DOWNLOAD_TIMEOUT = 120  # seconds

# Thread pool for running blocking subprocess calls (like gst-launch-1.0)
# without blocking the async event loop. Only 1 worker needed since we
# only ever capture one screenshot at a time.
_capture_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dcr_capture")


# =============================================================================
# Piper TTS Voice Registry — curated list of available voices
# =============================================================================
# These voices are NOT bundled in the plugin zip. They are downloaded on demand
# from HuggingFace and stored in DECKY_PLUGIN_SETTINGS_DIR/voices/ so they
# persist across plugin updates.
#
# URL pattern: https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/{lang_family}/{lang_code}/{speaker}/{quality}/{voice_id}.onnx
#
# Each voice has two files:
#   - {voice_id}.onnx       (~63MB, the ONNX model)
#   - {voice_id}.onnx.json  (~2KB, model config with sample rate, phoneme map, etc.)

PIPER_VOICE_BASE_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0"

PIPER_VOICES = {
    "en_US-amy-medium":       {"label": "US English - Amy (Female)",            "language": "English (US)",       "speakers": 1},
    "en_US-ryan-medium":      {"label": "US English - Ryan (Male)",             "language": "English (US)",       "speakers": 1},
    "en_GB-cori-medium":      {"label": "UK English - Cori (Female)",           "language": "English (UK)",       "speakers": 1},
    "en_GB-alan-medium":      {"label": "UK English - Alan (Male)",             "language": "English (UK)",       "speakers": 1},
    "de_DE-thorsten-medium":  {"label": "German - Thorsten (Male)",             "language": "German",             "speakers": 1},
    "es_ES-davefx-medium":    {"label": "Spanish (Spain) - Davefx",             "language": "Spanish (Spain)",    "speakers": 1},
    "es_MX-coconut-medium":   {"label": "Spanish (Mexico) - Coconut",           "language": "Spanish (Mexico)",   "speakers": 1},
    "fr_FR-gilles-medium":    {"label": "French - Gilles (Male)",               "language": "French",             "speakers": 1},
    "it_IT-paola-medium":     {"label": "Italian - Paola (Female)",              "language": "Italian",            "speakers": 1},
    "ja_JP-kokoro-medium":    {"label": "Japanese - Kokoro",                    "language": "Japanese",           "speakers": 1},
    "ko_KR-kss-medium":       {"label": "Korean - KSS",                        "language": "Korean",             "speakers": 1},
    "pl_PL-gosia-medium":     {"label": "Polish - Gosia (Female)",              "language": "Polish",             "speakers": 1},
    "pt_BR-edresson-medium":  {"label": "Portuguese (Brazil) - Edresson",       "language": "Portuguese (BR)",    "speakers": 1},
    "ru_RU-irina-medium":     {"label": "Russian - Irina (Female)",             "language": "Russian",            "speakers": 1},
    "uk_UA-ukrainian_tts-medium": {"label": "Ukrainian - Ukrainian TTS",        "language": "Ukrainian",          "speakers": 3, "speaker_id": 1},
    "zh_CN-huayan-medium":    {"label": "Chinese - Huayan",                     "language": "Chinese",            "speakers": 1},
}

# Default voice — auto-downloaded on first TTS use if not yet present.
DEFAULT_LOCAL_VOICE = "en_US-amy-medium"


def _piper_voice_url(voice_id, ext=".onnx"):
    """
    Construct the HuggingFace download URL for a Piper voice file.

    The URL structure is:
      {base}/{lang_family}/{lang_code}/{speaker}/{quality}/{voice_id}{ext}

    For example, voice_id="en_US-amy-medium" gives:
      .../en/en_US/amy/medium/en_US-amy-medium.onnx

    Args:
        voice_id: e.g., "en_US-amy-medium"
        ext: File extension, either ".onnx" or ".onnx.json"

    Returns:
        Full URL string for the voice file.
    """
    # Parse voice_id parts: "en_US-amy-medium" → lang_code="en_US", speaker="amy", quality="medium"
    parts = voice_id.split("-")
    lang_code = parts[0]              # "en_US"
    quality = parts[-1]               # "medium"
    speaker = "-".join(parts[1:-1])   # "amy" (handles multi-part names like "davefx")
    lang_family = lang_code.split("_")[0]  # "en"
    return f"{PIPER_VOICE_BASE_URL}/{lang_family}/{lang_code}/{speaker}/{quality}/{voice_id}{ext}"


# =============================================================================
# Default settings — used when no settings file exists yet, and to backfill
# any new keys added in future versions. Each key corresponds to a user-facing
# or internal configuration value.
# =============================================================================

DEFAULT_SETTINGS = {
    # Base64-encoded GCP service account JSON. Stored internally after the user
    # selects a JSON file via the file browser. Never shown to the user directly.
    "gcp_credentials_base64": "",

    # Provider selection: "gcp" (Google Cloud, online) or "local" (offline).
    # Default to local — works out of the box without setup.
    "ocr_provider": "local",
    "tts_provider": "local",

    # GCP Text-to-Speech voice ID. Format: "languageCode-Name".
    "voice_id": "en-US-Neural2-C",

    # GCP TTS speech rate preset. One of: x-slow, slow, medium, fast, x-fast.
    "speech_rate": "medium",

    # Local (Piper) TTS voice. Downloaded on demand from HuggingFace.
    "local_voice_id": "en_US-amy-medium",

    # Local (Piper) TTS speech rate preset. Same keys as GCP but maps to
    # Piper's length_scale (inverse: lower = faster).
    "local_speech_rate": "medium",

    # TTS volume level 0-100.
    "volume": 100,

    # Master on/off switch. When False: stops both workers + playback,
    # grays out OCR/TTS buttons, ignores background triggers (button hold).
    "enabled": True,

    # When True, extra diagnostic info is logged (useful for troubleshooting).
    "debug": False,

    # Which back button triggers the Read Screen pipeline without opening the UI.
    # Options: "disabled" (no button trigger), "L4", "R4", "L5", "R5".
    "trigger_button": "L4",

    # How long the trigger button must be held before the pipeline fires (ms).
    # Range: 300-1500ms. Higher values prevent accidental triggers.
    "hold_time_ms": 500,

    # Touchscreen input monitor (Phase 9). When True, reads touch events from
    # /dev/input/eventN to detect taps. Currently just logs coordinates —
    # future phases will add region selection and tap-to-read.
    "touchscreen_enabled": False,

    # Phase 10 — Capture mode for Phase 12. Determines how the screen region
    # is selected for OCR. "full_screen" captures the entire display.
    "capture_mode": "full_screen",

    # Phase 10 — When True, skip playing UI feedback sounds (Phase 11).
    "mute_interface_sounds": False,

    # Phase 10 — Fixed region coordinates for Phase 12 Fixed Region mode.
    # Defines a persistent bounding box the user can configure manually.
    # Defaults to full screen (0,0)-(1280,800).
    "fixed_region_x1": 0,
    "fixed_region_y1": 0,
    "fixed_region_x2": 1280,
    "fixed_region_y2": 800,

    # Phase 10 — Last selection coordinates, auto-saved by swipe/two-tap
    # modes in Phase 12. Can be applied to fixed_region via a UI button.
    # Defaults to full screen (0,0)-(1280,800).
    "last_selection_x1": 0,
    "last_selection_y1": 0,
    "last_selection_x2": 1280,
    "last_selection_y2": 800,

    # Phase 10 — Text filtering for Phase 13. Comma-separated word lists
    # that are stripped from OCR output before TTS.
    "ignored_words_always": "",
    "ignored_words_always_enabled": False,
    "ignored_words_beginning": "",
    "ignored_words_beginning_enabled": False,
    "ignored_words_count": 3,
}

# Fields that must be present in a valid GCP service account JSON file.
# If any of these are missing, the file is rejected.
REQUIRED_GCP_FIELDS = [
    "type",
    "project_id",
    "private_key_id",
    "private_key",
    "client_email",
]


# =============================================================================
# SettingsManager — reads/writes plugin settings to a JSON file
# =============================================================================
#
# Decky Loader provides a per-plugin settings directory via
# decky.DECKY_PLUGIN_SETTINGS_DIR. This class wraps simple JSON file I/O
# to persist settings across plugin restarts.
#
# Pattern borrowed from Decky-Translator's SettingsManager (main.py:519-555).
# =============================================================================

class SettingsManager:
    def __init__(self, name, settings_directory):
        """
        Initialize the settings manager.

        Args:
            name: Base name for the settings file (e.g., "settings" → "settings.json").
            settings_directory: Directory where the settings file is stored.
                                Typically decky.DECKY_PLUGIN_SETTINGS_DIR.
        """
        self.settings_path = os.path.join(settings_directory, f"{name}.json")
        self.settings = {}
        decky.logger.debug(f"{LOG} SettingsManager: path = {self.settings_path}")

    def read(self):
        """
        Load settings from the JSON file on disk into memory.
        If the file doesn't exist or is corrupt, settings start empty.
        """
        try:
            if os.path.exists(self.settings_path):
                with open(self.settings_path, "r") as f:
                    self.settings = json.load(f)
                decky.logger.debug(f"{LOG} SettingsManager: loaded from {self.settings_path}")
            else:
                decky.logger.info(f"{LOG} SettingsManager: no file yet at {self.settings_path}")
        except Exception as e:
            decky.logger.error(f"{LOG} SettingsManager: failed to read: {e}")
            decky.logger.error(traceback.format_exc())
            self.settings = {}

    def get(self, key, default=None):
        """
        Get a single setting value. Returns `default` if the key doesn't exist.
        """
        return self.settings.get(key, default)

    def set(self, key, value):
        """
        Set a single setting value and persist the entire settings dict to disk.
        Creates the settings directory if it doesn't exist.

        Returns True on success, False on error.
        """
        try:
            self.settings[key] = value
            # Ensure the directory exists (it should, but be safe)
            os.makedirs(os.path.dirname(self.settings_path), exist_ok=True)
            with open(self.settings_path, "w") as f:
                json.dump(self.settings, f, indent=4)
            decky.logger.debug(f"{LOG} SettingsManager: saved {key}")
            return True
        except Exception as e:
            decky.logger.error(f"{LOG} SettingsManager: failed to save {key}: {e}")
            decky.logger.error(traceback.format_exc())
            return False

    def get_all(self):
        """
        Return a copy of all current settings as a dict.
        """
        return dict(self.settings)


# =============================================================================
# Plugin class — the main Decky plugin backend
# =============================================================================

class Plugin:

    # =========================================================================
    # Lifecycle: _main()
    # =========================================================================
    # Called once when Decky Loader first loads this plugin.
    # We initialize the SettingsManager, load any saved settings from disk,
    # and backfill any new default keys that didn't exist in the saved file
    # (e.g., if we add a new setting in a future version).
    async def _main(self):
        decky.logger.info(f"{LOG} backend loaded")

        # Initialize the settings manager. It reads/writes a JSON file in the
        # plugin's dedicated settings directory.
        self.settings = SettingsManager("settings", decky.DECKY_PLUGIN_SETTINGS_DIR)
        self.settings.read()

        # Backfill defaults: if a key from DEFAULT_SETTINGS doesn't exist in
        # the saved file yet, add it with the default value. This handles
        # upgrades where we add new settings in a new plugin version.
        for key, default_value in DEFAULT_SETTINGS.items():
            if self.settings.get(key) is None:
                self.settings.set(key, default_value)
                decky.logger.debug(f"{LOG} backfilled default: {key}")

        # Storage for the last captured screenshot bytes. This is set by
        # capture_screenshot() and will be consumed by the OCR pipeline in
        # Phase 4. Keeping it on `self` avoids writing to disk unnecessarily.
        self._last_capture_bytes = None

        # Playback state: tracks the running mpv process (if any) and the
        # path to the temp MP3 file it's playing. Used by _start_playback()
        # and _stop_playback() to manage audio lifecycle.
        self._playback_process = None  # subprocess.Popen object or None
        self._tts_temp_path = None     # path to current MP3 temp file

        # Pipeline state: tracks the end-to-end Read Screen pipeline
        # (capture → OCR → TTS → playback). These fields let the frontend
        # poll progress and let stop_pipeline() cancel between steps.
        self._pipeline_step = "idle"              # Current step: idle/capturing/ocr/tts/playing/cancelled
        self._pipeline_cancel = threading.Event()  # Thread-safe cancellation flag
        self._pipeline_running = False             # Prevents concurrent pipelines

        # Phase 12: Capture mode state machine. All capture state is accessed
        # only from the event loop thread (via run_coroutine_threadsafe and
        # call_later) — no lock needed.
        self._capture_state = "idle"              # "idle" | "waiting_second_tap"
        self._first_tap_x = 0                     # First tap X for two-tap mode
        self._first_tap_y = 0                     # First tap Y for two-tap mode
        self._two_tap_timer = None                # asyncio TimerHandle for 5s timeout
        self._touch_started_during_playback = False  # Prevents post-stop capture

        # Persistent GCP worker subprocess state.
        # Instead of spawning a new subprocess for every OCR/TTS call (paying
        # ~1.7s of Python startup + imports + client init each time), we keep
        # a single gcp_worker.py process alive in "serve" mode. It initializes
        # GCP clients once at startup and reuses them for every request.
        # Communication is via stdin/stdout JSON lines.
        self._worker_process = None              # subprocess.Popen or None
        self._worker_lock = threading.Lock()     # Serializes stdin/stdout access
        self._worker_stderr_thread = None        # Daemon thread draining stderr

        # Persistent LOCAL worker subprocess state (mirrors GCP worker above).
        # Runs local_worker.py under the bundled Python 3.12 interpreter.
        # Uses RapidOCR + Piper TTS for offline inference.
        self._local_worker_process = None        # subprocess.Popen or None
        self._local_worker_lock = threading.Lock()
        self._local_worker_stderr_thread = None

        decky.logger.info(f"{LOG} settings initialized")

        # Sync the Python logger level to the "debug" setting stored in the
        # JSON config.  Decky Loader defaults the logger to INFO, so
        # decky.logger.debug() messages are silently dropped unless we
        # explicitly lower the level to DEBUG here.
        if self.settings.get("debug", DEFAULT_SETTINGS["debug"]):
            decky.logger.setLevel(logging.DEBUG)
            decky.logger.info(f"{LOG} debug logging enabled")
        else:
            decky.logger.setLevel(logging.INFO)

        # -----------------------------------------------------------------
        # Discover system Python for running gcp_worker.py
        # -----------------------------------------------------------------
        # Decky's embedded Python can't load google-cloud native libs (C
        # extensions). We need the system Python (/usr/bin/python3) to run
        # gcp_worker.py as a subprocess. Here we find the first working
        # Python from a list of candidates and store the path for later use.
        #
        # We DON'T crash if not found — OCR/TTS will fail gracefully with
        # a clear error message when the user tries to use them.
        self._system_python = None
        python_candidates = [
            "/usr/bin/python3",
            "/usr/bin/python3.13",
            "/usr/bin/python3.12",
            "/usr/bin/python3.11",
        ]
        for candidate in python_candidates:
            try:
                result = subprocess.run(
                    [candidate, "--version"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode == 0:
                    version = result.stdout.strip() or result.stderr.strip()
                    self._system_python = candidate
                    decky.logger.info(f"{LOG} system Python found: {candidate} ({version})")
                    break
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue

        if not self._system_python:
            decky.logger.warning(f"{LOG} no system Python found — OCR/TTS will not work")

        # Resolve paths to gcp_worker.py and py_modules/ relative to the
        # plugin directory. These are set at install time by the Dockerfile.
        plugin_dir = decky.DECKY_PLUGIN_DIR
        self._gcp_worker_path = os.path.join(plugin_dir, "gcp_worker.py")
        self._py_modules_path = os.path.join(plugin_dir, "py_modules")

        if os.path.exists(self._gcp_worker_path):
            decky.logger.info(f"{LOG} gcp_worker.py found: {self._gcp_worker_path}")
        else:
            decky.logger.warning(f"{LOG} gcp_worker.py NOT found at {self._gcp_worker_path}")

        if os.path.isdir(self._py_modules_path):
            decky.logger.info(f"{LOG} py_modules found: {self._py_modules_path}")
        else:
            decky.logger.warning(f"{LOG} py_modules NOT found at {self._py_modules_path}")

        # -----------------------------------------------------------------
        # Discover bundled Python 3.12 for running local_worker.py
        # -----------------------------------------------------------------
        # The bundled Python 3.12 is used for local OCR/TTS inference because
        # rapidocr-onnxruntime doesn't support Python 3.13. It's downloaded
        # during the Docker build from python-build-standalone.
        self._local_python_path = None
        self._local_worker_script = None
        self._local_models_dir = None
        self._local_py_modules_path = None

        candidate = os.path.join(plugin_dir, "python312", "python", "bin", "python3.12")
        if os.path.exists(candidate) and os.access(candidate, os.X_OK):
            self._local_python_path = candidate
            decky.logger.info(f"{LOG} bundled Python 3.12 found: {candidate}")
        else:
            decky.logger.warning(f"{LOG} bundled Python 3.12 NOT found at {candidate} — local OCR/TTS unavailable")

        self._local_worker_script = os.path.join(plugin_dir, "local_worker.py")
        if os.path.exists(self._local_worker_script):
            decky.logger.info(f"{LOG} local_worker.py found: {self._local_worker_script}")
        else:
            decky.logger.warning(f"{LOG} local_worker.py NOT found at {self._local_worker_script}")

        self._local_models_dir = os.path.join(plugin_dir, "models")
        if os.path.isdir(self._local_models_dir):
            decky.logger.info(f"{LOG} models dir found: {self._local_models_dir}")
        else:
            decky.logger.warning(f"{LOG} models dir NOT found at {self._local_models_dir}")

        self._local_py_modules_path = os.path.join(plugin_dir, "py_modules_local")
        if os.path.isdir(self._local_py_modules_path):
            decky.logger.info(f"{LOG} py_modules_local found: {self._local_py_modules_path}")
        else:
            decky.logger.warning(f"{LOG} py_modules_local NOT found at {self._local_py_modules_path}")

        # -----------------------------------------------------------------
        # Set up voices directory for on-demand Piper TTS voice downloads
        # -----------------------------------------------------------------
        # Voice models are stored in the settings directory (not the plugin
        # directory) so they persist across plugin updates. The plugin dir
        # is wiped on every install, but settings dir is preserved.
        self._voices_dir = os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "voices")
        os.makedirs(self._voices_dir, exist_ok=True)
        decky.logger.info(f"{LOG} voices dir: {self._voices_dir}")

        # Clean up any partial downloads (.tmp files) left from crashes
        for tmp_file in glob.glob(os.path.join(self._voices_dir, "*.tmp")):
            try:
                os.remove(tmp_file)
                decky.logger.debug(f"{LOG} cleaned up partial download: {tmp_file}")
            except OSError:
                pass

        # -----------------------------------------------------------------
        # Discover audio player for TTS playback
        # -----------------------------------------------------------------
        # We try several players in priority order. mpv is ideal but not
        # always installed. ffplay and pw-play are reliably present on
        # Steam Deck. Each player needs different CLI flags, so we store
        # both the path and the player name for command construction.
        self._audio_player_path = None
        self._audio_player_name = None  # "mpv", "ffplay", or "pw-play"
        audio_candidates = [
            ("mpv", "mpv"),
            ("ffplay", "ffplay"),
            ("pw-play", "pw-play"),
        ]
        for name, binary in audio_candidates:
            path = shutil.which(binary)
            if path:
                self._audio_player_path = path
                self._audio_player_name = name
                decky.logger.info(f"{LOG} audio player found: {name} at {path}")
                break

        if not self._audio_player_path:
            decky.logger.warning(f"{LOG} no audio player found (tried mpv, ffplay, pw-play) — TTS playback will not work")

        # -----------------------------------------------------------------
        # Capture the event loop for cross-thread async dispatch
        # -----------------------------------------------------------------
        # The hidraw monitor runs in a daemon thread. When it detects a
        # button hold, it needs to call read_screen() which is async. We
        # use asyncio.run_coroutine_threadsafe() to schedule the coroutine
        # on the main event loop. This requires a reference to the loop.
        self._event_loop = asyncio.get_event_loop()

        # -----------------------------------------------------------------
        # Start hidraw button monitor
        # -----------------------------------------------------------------
        # The monitor runs in a background thread, reading HID packets from
        # the Steam Deck controller. When the user holds the configured
        # button for the threshold duration, it calls _on_button_trigger().
        # Graceful degradation: if the device isn't found (e.g., running on
        # a dev machine), the plugin still works via the UI.
        self._hidraw_monitor = None
        trigger_button = self.settings.get("trigger_button", DEFAULT_SETTINGS["trigger_button"])
        hold_time_ms = self.settings.get("hold_time_ms", DEFAULT_SETTINGS["hold_time_ms"])

        if trigger_button != "disabled":
            self._hidraw_monitor = HidrawButtonMonitor(
                target_button=trigger_button,
                hold_threshold_ms=hold_time_ms,
                on_trigger=self._on_button_trigger,
                logger=decky.logger,
                log_prefix=LOG,
            )
            started = self._hidraw_monitor.start()
            if started:
                decky.logger.info(f"{LOG} button monitor started: button={trigger_button}, hold={hold_time_ms}ms")
            else:
                decky.logger.warning(f"{LOG} button monitor failed to start — trigger disabled (UI still works)")
        else:
            decky.logger.info(f"{LOG} button trigger disabled by settings")

        # -----------------------------------------------------------------
        # Start touchscreen monitor (Phase 12: auto-managed by capture mode)
        # -----------------------------------------------------------------
        # The touchscreen monitor is automatically started/stopped based on
        # the capture_mode setting. Modes that need touch input (swipe,
        # two_tap, hybrid) auto-start it; others (full_screen, fixed_region)
        # leave it stopped. No manual toggle needed.
        self._touchscreen_monitor = None
        self._sync_touchscreen_for_mode()
        decky.logger.info(f"{LOG} touchscreen monitor synced for capture_mode={self.settings.get('capture_mode', 'full_screen')}")

    # =========================================================================
    # Button trigger: _on_button_trigger() / _handle_button_trigger()
    # =========================================================================
    # _on_button_trigger() is called from the hidraw monitor thread when the
    # configured button is held past the threshold. It dispatches to
    # _handle_button_trigger() on the async event loop.

    def _on_button_trigger(self):
        """
        Called from the hidraw monitor thread when the hold threshold is met.

        This method bridges the synchronous monitor thread to the async event
        loop by scheduling _handle_button_trigger() as a coroutine. We use
        run_coroutine_threadsafe() which is the standard way to call async
        code from a non-async thread.
        """
        asyncio.run_coroutine_threadsafe(
            self._handle_button_trigger(), self._event_loop
        )

    @property
    def _is_enabled(self):
        """Check if the plugin's master switch is on."""
        return self.settings.get("enabled", True)

    @property
    def _is_playing(self):
        """Check if audio is currently playing."""
        return self._playback_process is not None and self._playback_process.poll() is None

    async def _handle_button_trigger(self):
        """
        Runs on the event loop — guards and triggers the Read Screen pipeline.

        Phase 12: Mode-aware button handling.
          - If playing/running → stop and return (all modes)
          - swipe_selection / two_tap_selection → button only stops, never starts
          - full_screen → no crop
          - fixed_region / hybrid → use fixed region crop

        Guards:
          - Plugin must be enabled (settings.enabled)
          - Providers must be available (GCP needs creds, local needs bundled Python)
        """
        # Guard: plugin must be enabled
        if not self._is_enabled:
            decky.logger.debug(f"{LOG} button trigger: plugin disabled, ignoring")
            return

        # Guard: if playing or pipeline running, stop and return (all modes)
        if self._is_playing or self._pipeline_running:
            decky.logger.info(f"{LOG} button trigger: stopping playback/pipeline")
            await self._stop_and_sound()
            return

        mode = self.settings.get("capture_mode", DEFAULT_SETTINGS["capture_mode"])

        # In swipe/two-tap only modes, button only stops — never starts pipeline
        if mode in ("swipe_selection", "two_tap_selection"):
            decky.logger.debug(f"{LOG} button trigger: button only stops in {mode} mode")
            return

        # Guard: don't start a second pipeline
        if self._pipeline_running:
            decky.logger.debug(f"{LOG} button trigger: pipeline already running, ignoring")
            return

        # Guard: check that providers are available
        ocr_provider = self.settings.get("ocr_provider", DEFAULT_SETTINGS["ocr_provider"])
        tts_provider = self.settings.get("tts_provider", DEFAULT_SETTINGS["tts_provider"])

        if ocr_provider == "gcp" or tts_provider == "gcp":
            creds_b64 = self.settings.get("gcp_credentials_base64", "")
            if not creds_b64:
                decky.logger.debug(f"{LOG} button trigger: GCP provider selected but no credentials, ignoring")
                return

        if ocr_provider == "local" or tts_provider == "local":
            if not self._local_python_path:
                decky.logger.debug(f"{LOG} button trigger: local provider selected but bundled Python unavailable, ignoring")
                return

        # Mode-specific pipeline start
        if mode == "full_screen":
            decky.logger.info(f"{LOG} button trigger: full_screen mode — starting pipeline")
            self._play_interface_sound("selection_end")
            result = await self._read_screen_with_crop()
        elif mode in ("fixed_region", "hybrid"):
            crop = self._get_fixed_region_crop()
            decky.logger.info(f"{LOG} button trigger: {mode} mode — crop={crop}")
            self._play_interface_sound("selection_end")
            result = await self._read_screen_with_crop(crop_region=crop)
        else:
            # Fallback (shouldn't reach here, but be safe)
            self._play_interface_sound("selection_end")
            result = await self._read_screen_with_crop()

        decky.logger.info(f"{LOG} button trigger result: {result.get('message', '')}")

    # =========================================================================
    # Phase 12: Touch callbacks — bridge monitor thread to event loop
    # =========================================================================
    # Three callbacks from the TouchscreenMonitor thread. Each dispatches an
    # async handler onto the event loop via run_coroutine_threadsafe().

    def _on_touch_down(self, x, y):
        """From monitor thread: finger made contact at (x, y)."""
        if not self._is_enabled:
            return
        asyncio.run_coroutine_threadsafe(
            self._handle_touch_down(x, y), self._event_loop
        )

    def _on_touch_up(self, end_x, end_y, start_x, start_y, duration):
        """From monitor thread: finger lifted. Provides start/end coords + duration."""
        if not self._is_enabled:
            return
        asyncio.run_coroutine_threadsafe(
            self._handle_touch_up(end_x, end_y, start_x, start_y, duration),
            self._event_loop,
        )

    def _on_touch_tap(self, x, y):
        """From monitor thread: short tap detected (legacy, < 0.5s)."""
        if not self._is_enabled:
            return
        asyncio.run_coroutine_threadsafe(
            self._handle_touch_tap(x, y), self._event_loop
        )

    # =========================================================================
    # Phase 12: Async touch handlers (run on event loop)
    # =========================================================================

    async def _handle_touch_down(self, x, y):
        """
        Handle finger-down event. Used by swipe mode to play start sound.
        In all modes: if playing or pipeline running, mark as stop gesture.
        """
        mode = self.settings.get("capture_mode", DEFAULT_SETTINGS["capture_mode"])
        decky.logger.debug(f"{LOG} touch_down: ({x},{y}) mode={mode}")

        # During playback or pipeline: mark this touch as a stop gesture
        if self._is_playing or self._pipeline_running:
            self._touch_started_during_playback = True
            await self._stop_and_sound()
            return

        # Swipe mode: play start sound on finger contact
        if mode == "swipe_selection":
            self._touch_started_during_playback = False
            self._play_interface_sound("selection_start")

    async def _handle_touch_up(self, end_x, end_y, start_x, start_y, duration):
        """
        Handle finger-up event. Used by swipe mode to define selection region.
        """
        mode = self.settings.get("capture_mode", DEFAULT_SETTINGS["capture_mode"])
        decky.logger.debug(
            f"{LOG} touch_up: start=({start_x},{start_y}) end=({end_x},{end_y}) "
            f"dur={duration:.2f}s mode={mode}"
        )

        # If this touch started during playback, it was a stop gesture — don't start pipeline
        if self._touch_started_during_playback:
            self._touch_started_during_playback = False
            return

        # Safety: if playing or running, stop (shouldn't normally reach here)
        if self._is_playing or self._pipeline_running:
            await self._stop_and_sound()
            return

        # Swipe selection mode: use start/end coordinates as bounding box
        if mode == "swipe_selection":
            # Normalize coordinates (min/max)
            x1 = min(start_x, end_x)
            y1 = min(start_y, end_y)
            x2 = max(start_x, end_x)
            y2 = max(start_y, end_y)

            # Check minimum selection size (50x50 pixels)
            if (x2 - x1) < 50 or (y2 - y1) < 50:
                decky.logger.debug(
                    f"{LOG} swipe too small: {x2 - x1}x{y2 - y1}, ignoring"
                )
                return

            # Save selection coordinates for Fixed Region / Hybrid modes
            self.settings.set("last_selection_x1", x1)
            self.settings.set("last_selection_y1", y1)
            self.settings.set("last_selection_x2", x2)
            self.settings.set("last_selection_y2", y2)

            self._play_interface_sound("selection_end")
            crop = {"x1": x1, "y1": y1, "x2": x2, "y2": y2}
            decky.logger.info(f"{LOG} swipe selection: crop={crop}")
            await self._read_screen_with_crop(crop_region=crop)

    async def _handle_touch_tap(self, x, y):
        """
        Handle short tap event (< 0.5s). Used by two-tap and hybrid modes
        to define rectangle corners.
        """
        mode = self.settings.get("capture_mode", DEFAULT_SETTINGS["capture_mode"])
        decky.logger.debug(f"{LOG} touch_tap: ({x},{y}) mode={mode}")

        # During playback or pipeline: stop
        if self._is_playing or self._pipeline_running:
            await self._stop_and_sound()
            return

        # Two-tap and hybrid modes: two taps define a rectangle
        if mode in ("two_tap_selection", "hybrid"):
            if self._capture_state == "idle":
                # First tap: record position, start 5s timeout
                self._play_interface_sound("selection_start")
                self._first_tap_x = x
                self._first_tap_y = y
                self._capture_state = "waiting_second_tap"
                # Start 5-second timeout via event loop
                self._two_tap_timer = self._event_loop.call_later(
                    5.0, self._two_tap_timeout
                )
                decky.logger.info(f"{LOG} two-tap: first tap at ({x},{y}), waiting for second...")

            elif self._capture_state == "waiting_second_tap":
                # Second tap: cancel timer, compute region, start pipeline
                if self._two_tap_timer:
                    self._two_tap_timer.cancel()
                    self._two_tap_timer = None
                self._capture_state = "idle"

                # Normalize coordinates (min/max)
                x1 = min(self._first_tap_x, x)
                y1 = min(self._first_tap_y, y)
                x2 = max(self._first_tap_x, x)
                y2 = max(self._first_tap_y, y)

                # Save selection coordinates
                self.settings.set("last_selection_x1", x1)
                self.settings.set("last_selection_y1", y1)
                self.settings.set("last_selection_x2", x2)
                self.settings.set("last_selection_y2", y2)

                self._play_interface_sound("selection_end")
                crop = {"x1": x1, "y1": y1, "x2": x2, "y2": y2}
                decky.logger.info(f"{LOG} two-tap: second tap at ({x},{y}), crop={crop}")
                await self._read_screen_with_crop(crop_region=crop)

    def _two_tap_timeout(self):
        """
        Called by event loop's call_later when 5s expires without a second tap.
        Resets capture state and plays stop sound.
        """
        if self._capture_state == "waiting_second_tap":
            self._capture_state = "idle"
            self._two_tap_timer = None
            self._play_interface_sound("stop")
            decky.logger.info(f"{LOG} two-tap: timeout, cancelled")

    # =========================================================================
    # Phase 12: Helper methods
    # =========================================================================

    async def _stop_and_sound(self):
        """Stop playback/pipeline and play the stop sound."""
        self._play_interface_sound("stop")
        self._pipeline_cancel.set()
        self._stop_playback()

    def _get_fixed_region_crop(self):
        """
        Read fixed_region coordinates from settings, clamp to screen bounds,
        normalize min/max, and return as a crop dict.
        """
        x1 = max(0, min(int(self.settings.get("fixed_region_x1", 0)), 1280))
        y1 = max(0, min(int(self.settings.get("fixed_region_y1", 0)), 800))
        x2 = max(0, min(int(self.settings.get("fixed_region_x2", 1280)), 1280))
        y2 = max(0, min(int(self.settings.get("fixed_region_y2", 800)), 800))
        # Normalize
        if x1 > x2:
            x1, x2 = x2, x1
        if y1 > y2:
            y1, y2 = y2, y1
        return {"x1": x1, "y1": y1, "x2": x2, "y2": y2}

    def _sync_touchscreen_for_mode(self, mode=None):
        """
        Auto-start or stop the touchscreen monitor based on capture mode.

        Modes that need touch: swipe_selection, two_tap_selection, hybrid
        Modes that don't: full_screen, fixed_region
        """
        if mode is None:
            mode = self.settings.get("capture_mode", DEFAULT_SETTINGS["capture_mode"])

        needs_touch = mode in ("swipe_selection", "two_tap_selection", "hybrid")

        if needs_touch and self._touchscreen_monitor is None:
            # Start touchscreen monitor with all 3 callbacks
            self._touchscreen_monitor = TouchscreenMonitor(
                on_touch=self._on_touch_tap,
                on_touch_down=self._on_touch_down,
                on_touch_up=self._on_touch_up,
                logger=decky.logger,
                log_prefix=LOG,
            )
            started = self._touchscreen_monitor.start()
            if started:
                decky.logger.info(f"{LOG} touchscreen auto-started for {mode} mode")
            else:
                decky.logger.warning(f"{LOG} touchscreen failed to start for {mode} mode")
        elif not needs_touch and self._touchscreen_monitor is not None:
            # Stop touchscreen monitor — not needed for this mode
            self._touchscreen_monitor.stop()
            self._touchscreen_monitor = None
            decky.logger.info(f"{LOG} touchscreen auto-stopped for {mode} mode")

    # =========================================================================
    # Lifecycle: _unload()
    # =========================================================================
    # Called when the plugin is stopped (e.g., Decky Loader restarts, or the
    # user disables the plugin). The plugin is NOT removed from disk.
    async def _unload(self):
        # Step 0a: Stop the hidraw button monitor (stops thread, closes device FD)
        if self._hidraw_monitor:
            self._hidraw_monitor.stop()
            self._hidraw_monitor = None
            decky.logger.info(f"{LOG} button monitor stopped")

        # Step 0a2: Stop the touchscreen monitor (stops thread, closes device FD)
        if self._touchscreen_monitor:
            self._touchscreen_monitor.stop()
            self._touchscreen_monitor = None
            decky.logger.info(f"{LOG} touchscreen monitor stopped")

        # Step 0a3: Cancel any pending two-tap timer (Phase 12)
        if self._two_tap_timer:
            self._two_tap_timer.cancel()
            self._two_tap_timer = None

        # Step 0b: Cancel any running pipeline so it stops between steps
        self._pipeline_cancel.set()
        decky.logger.info(f"{LOG} pipeline cancel flag set")

        # Step 0c: Stop the persistent GCP worker subprocess
        self._stop_worker()
        decky.logger.info(f"{LOG} GCP worker stopped")

        # Step 0d: Stop the persistent local worker subprocess
        self._stop_local_worker()
        decky.logger.info(f"{LOG} local worker stopped")

        # Step 1: Stop any running audio playback and clean up its temp file
        self._stop_playback()
        decky.logger.info(f"{LOG} playback stopped")

        # Step 2: Shut down the thread pool executor. wait=False so we don't
        # block the unload if a capture is somehow still running.
        _capture_executor.shutdown(wait=False)
        decky.logger.info(f"{LOG} capture executor shut down")

        # Step 3: Sweep any orphaned temp files from previous runs.
        # These could exist if the plugin crashed mid-pipeline.
        for pattern in ["/tmp/dcr_*.png", "/tmp/dcr_*.mp3", "/tmp/dcr_*.wav"]:
            for orphan in glob.glob(pattern):
                try:
                    os.remove(orphan)
                    decky.logger.debug(f"{LOG} swept orphaned temp file: {orphan}")
                except OSError:
                    pass

        decky.logger.info(f"{LOG} backend unloaded")

    # =========================================================================
    # Lifecycle: _uninstall()
    # =========================================================================
    # Called after _unload() when the plugin is fully removed from disk.
    async def _uninstall(self):
        decky.logger.info(f"{LOG} backend uninstalled")

    # =========================================================================
    # Screen capture: _capture_screenshot_sync() (internal helper)
    # =========================================================================
    # This is a SYNCHRONOUS method that runs the GStreamer pipeline to capture
    # a single screenshot from PipeWire. It must be run in a thread pool (not
    # on the async event loop) because subprocess.run() blocks.
    #
    # The GStreamer pipeline:
    #   pipewiresrc  — reads frames from PipeWire (the Steam Deck's display server)
    #   videoconvert — converts pixel formats (PipeWire may output various formats)
    #   pngenc       — encodes the frame as PNG (snapshot=true = only last frame)
    #   filesink     — writes the PNG to a temp file
    #
    # We use num-buffers=5 instead of 1 because the first few frames from
    # PipeWire can be blank or invalid. By capturing 5 frames and using
    # snapshot=true on pngenc, we reliably get a valid screenshot.
    # This pattern comes from the Decky-Translator reference plugin.
    def _capture_screenshot_sync(self):
        """
        Capture a screenshot via GStreamer + PipeWire. Returns a dict:
          {success: bool, image_bytes: bytes|None, file_size: int, error: str|None}
        """
        # Step 1: Find gst-launch-1.0 on the system PATH.
        # On Steam Deck it's at /usr/bin/gst-launch-1.0 (pre-installed).
        gst_path = shutil.which("gst-launch-1.0")
        if not gst_path:
            return {
                "success": False,
                "image_bytes": None,
                "file_size": 0,
                "error": "gst-launch-1.0 not found on PATH",
            }

        # Step 2: Create a secure temp file for the screenshot output.
        # mkstemp returns (file_descriptor, path). We close the fd immediately
        # since GStreamer will write to the path directly.
        fd, tmp_path = tempfile.mkstemp(suffix=".png", prefix="dcr_capture_")
        os.close(fd)

        try:
            # Step 3: Build the GStreamer pipeline command.
            # -e  = send EOS (end-of-stream) on interrupt for clean shutdown
            cmd = [
                gst_path, "-e",
                "pipewiresrc", "do-timestamp=true", "num-buffers=5",
                "!", "videoconvert",
                "!", "pngenc", "snapshot=true",
                "!", "filesink", f"location={tmp_path}",
            ]

            # Step 4: Set environment variables required for PipeWire access.
            # XDG_RUNTIME_DIR tells PipeWire where its socket is.
            # XDG_SESSION_TYPE tells it we're running under Wayland.
            env = os.environ.copy()
            env["XDG_RUNTIME_DIR"] = "/run/user/1000"
            env["XDG_SESSION_TYPE"] = "wayland"

            decky.logger.info(f"{LOG} capturing screenshot to {tmp_path}")
            decky.logger.debug(f"{LOG} capture command: {' '.join(cmd)}")

            # Step 5: Run the GStreamer pipeline as a subprocess.
            # capture_output=True captures stdout/stderr for error reporting.
            result = subprocess.run(
                cmd,
                env=env,
                timeout=CAPTURE_TIMEOUT,
                capture_output=True,
            )

            # Step 6: Check if the command succeeded (exit code 0).
            if result.returncode != 0:
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                decky.logger.error(f"{LOG} gst-launch failed (code {result.returncode}): {stderr}")
                return {
                    "success": False,
                    "image_bytes": None,
                    "file_size": 0,
                    "error": f"GStreamer failed (code {result.returncode}): {stderr[:200]}",
                }

            # Step 7: Read and validate the output file.
            if not os.path.exists(tmp_path):
                return {
                    "success": False,
                    "image_bytes": None,
                    "file_size": 0,
                    "error": "Screenshot file was not created",
                }

            file_size = os.path.getsize(tmp_path)
            if file_size == 0:
                return {
                    "success": False,
                    "image_bytes": None,
                    "file_size": 0,
                    "error": "Screenshot file is empty (0 bytes)",
                }

            # Step 8: Read the PNG bytes into memory.
            with open(tmp_path, "rb") as f:
                image_bytes = f.read()

            decky.logger.info(f"{LOG} screenshot captured: {file_size} bytes")
            return {
                "success": True,
                "image_bytes": image_bytes,
                "file_size": file_size,
                "error": None,
            }

        except subprocess.TimeoutExpired:
            decky.logger.error(f"{LOG} capture timed out after {CAPTURE_TIMEOUT}s")
            return {
                "success": False,
                "image_bytes": None,
                "file_size": 0,
                "error": f"Capture timed out after {CAPTURE_TIMEOUT} seconds",
            }
        except Exception as e:
            decky.logger.error(f"{LOG} capture error: {e}")
            decky.logger.error(traceback.format_exc())
            return {
                "success": False,
                "image_bytes": None,
                "file_size": 0,
                "error": str(e),
            }
        finally:
            # Always clean up the temp file, even on error.
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                    decky.logger.debug(f"{LOG} cleaned up temp file: {tmp_path}")
            except OSError as e:
                decky.logger.warning(f"{LOG} failed to clean up {tmp_path}: {e}")

    # =========================================================================
    # RPC: capture_screenshot()
    # =========================================================================
    # Async wrapper around _capture_screenshot_sync(). Runs the blocking
    # GStreamer subprocess in a thread pool so it doesn't freeze the event loop.
    #
    # Returns a dict to the frontend:
    #   {success: bool, file_size: int, message: str}
    #
    # Also stores the captured image bytes on self._last_capture_bytes for
    # use by the OCR pipeline in Phase 4.
    #
    # Called from the frontend via:
    #   const captureScreenshot = callable<[], CaptureResult>("capture_screenshot");
    async def capture_screenshot(self):
        decky.logger.info(f"{LOG} capture_screenshot() called")

        # Run the blocking capture in the thread pool executor.
        # asyncio.get_event_loop().run_in_executor() schedules the sync function
        # on the executor and returns an awaitable future.
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(_capture_executor, self._capture_screenshot_sync)

        if result["success"]:
            # Store the image bytes for later use by OCR (Phase 4)
            self._last_capture_bytes = result["image_bytes"]
            return {
                "success": True,
                "file_size": result["file_size"],
                "message": f"Screenshot captured: {result['file_size']:,} bytes",
            }
        else:
            self._last_capture_bytes = None
            return {
                "success": False,
                "file_size": 0,
                "message": result["error"],
            }

    # =========================================================================
    # Persistent GCP worker: _start_worker / _stop_worker / _send_to_worker
    # =========================================================================
    # Instead of spawning a new subprocess for every OCR/TTS request (paying
    # ~1.7s each time for Python startup + imports + client init), we keep a
    # single gcp_worker.py running in "serve" mode. It initializes GCP clients
    # once at startup and processes requests via stdin/stdout JSON lines.
    #
    # The worker is started lazily on first use and restarted automatically
    # if it dies. Credential changes trigger a restart so the worker picks
    # up new credentials.

    def _start_worker(self):
        """
        Launch the persistent gcp_worker.py subprocess in serve mode.

        Pre-flight checks: system Python, gcp_worker.py, and credentials must
        exist. The subprocess reads GCP_CREDENTIALS_BASE64 from its environment
        at startup and initializes both Vision + TTS clients once.

        After launch, waits for the {"ready": true} signal from the worker.
        Starts a daemon thread to drain stderr (worker diagnostic logs).

        Returns:
            True if the worker started and signaled ready, False otherwise.
        """
        # Pre-flight checks
        if not self._system_python:
            decky.logger.error(f"{LOG} worker: system Python not found — cannot start")
            return False

        if not os.path.exists(self._gcp_worker_path):
            decky.logger.error(f"{LOG} worker: gcp_worker.py not found at {self._gcp_worker_path}")
            return False

        creds_b64 = self.settings.get("gcp_credentials_base64", "")
        if not creds_b64:
            decky.logger.error(f"{LOG} worker: no GCP credentials configured")
            return False

        # Build subprocess environment
        env = os.environ.copy()
        env["PYTHONPATH"] = self._py_modules_path
        env["PYTHONNOUSERSITE"] = "1"
        env["GCP_CREDENTIALS_BASE64"] = creds_b64

        cmd = [self._system_python, self._gcp_worker_path, "serve"]
        decky.logger.info(f"{LOG} worker: starting persistent subprocess...")

        try:
            self._worker_process = subprocess.Popen(
                cmd,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,  # Line-buffered for JSON line protocol
            )
        except Exception as e:
            decky.logger.error(f"{LOG} worker: failed to launch: {e}")
            self._worker_process = None
            return False

        # Start a daemon thread to drain stderr. Without this, the stderr pipe
        # buffer fills up and the worker blocks trying to write diagnostic logs.
        self._worker_stderr_thread = threading.Thread(
            target=self._drain_worker_stderr,
            daemon=True,
            name="dcr_worker_stderr",
        )
        self._worker_stderr_thread.start()

        # Wait for the ready signal — the first line the worker writes to stdout.
        # We use a reader thread + join(timeout) so we don't block forever if
        # the worker hangs during initialization.
        ready_result = [None]  # Mutable container for thread result

        def _read_ready():
            try:
                line = self._worker_process.stdout.readline()
                if line:
                    ready_result[0] = json.loads(line.strip())
            except Exception as e:
                ready_result[0] = {"ready": False, "message": f"Ready read error: {e}"}

        reader = threading.Thread(target=_read_ready, daemon=True)
        reader.start()
        reader.join(timeout=30)  # 30s should be plenty for imports + client init

        if reader.is_alive():
            # Timed out waiting for ready signal
            decky.logger.error(f"{LOG} worker: timed out waiting for ready signal (30s)")
            self._stop_worker()
            return False

        ready = ready_result[0]
        if ready is None or not ready.get("ready", False):
            msg = ready.get("message", "unknown") if ready else "no response"
            decky.logger.error(f"{LOG} worker: not ready: {msg}")
            self._stop_worker()
            return False

        decky.logger.info(f"{LOG} worker: persistent worker ready (pid={self._worker_process.pid})")
        return True

    def _stop_worker(self):
        """
        Stop the persistent GCP worker subprocess gracefully.

        Shutdown sequence:
          1. Send {"action": "shutdown"} via stdin (graceful exit)
          2. wait(timeout=3) for the process to exit
          3. SIGTERM + wait(timeout=2) if still alive
          4. SIGKILL as last resort

        Safe to call when worker is None or already dead — it's a no-op.
        """
        if self._worker_process is None:
            return

        pid = self._worker_process.pid

        try:
            # Step 1: Try graceful shutdown via the JSON protocol
            if self._worker_process.poll() is None:
                try:
                    self._worker_process.stdin.write('{"action":"shutdown"}\n')
                    self._worker_process.stdin.flush()
                except (BrokenPipeError, OSError):
                    pass  # Pipe already closed — process may have crashed

                try:
                    self._worker_process.wait(timeout=3)
                    decky.logger.info(f"{LOG} worker: stopped gracefully (pid={pid})")
                except subprocess.TimeoutExpired:
                    # Step 2: SIGTERM
                    decky.logger.warning(f"{LOG} worker: graceful shutdown timed out, sending SIGTERM")
                    try:
                        self._worker_process.send_signal(signal.SIGTERM)
                        self._worker_process.wait(timeout=2)
                        decky.logger.info(f"{LOG} worker: stopped via SIGTERM (pid={pid})")
                    except subprocess.TimeoutExpired:
                        # Step 3: SIGKILL as last resort
                        decky.logger.warning(f"{LOG} worker: SIGTERM timed out, sending SIGKILL")
                        self._worker_process.kill()
                        self._worker_process.wait(timeout=2)
                    except ProcessLookupError:
                        pass  # Already exited between checks
            else:
                decky.logger.debug(f"{LOG} worker: already exited (pid={pid})")

        except ProcessLookupError:
            decky.logger.debug(f"{LOG} worker: process already gone (pid={pid})")

        except Exception as e:
            decky.logger.error(f"{LOG} worker: error stopping: {e}")

        finally:
            # Close all pipes and reset state
            for pipe in (self._worker_process.stdin, self._worker_process.stdout, self._worker_process.stderr):
                try:
                    if pipe:
                        pipe.close()
                except Exception:
                    pass
            self._worker_process = None
            self._worker_stderr_thread = None

    def _send_to_worker(self, command_dict, timeout=OCR_TIMEOUT):
        """
        Send a command to the persistent worker and wait for a response.

        Lazy start/restart: if the worker isn't running, starts it automatically.
        If the worker crashes between requests, the next call restarts it.

        Thread-safe: serializes access via _worker_lock so concurrent callers
        (e.g., pipeline + standalone OCR) don't interleave stdin/stdout.

        Args:
            command_dict: Dict to send as JSON (e.g., {"action": "ocr", "image_path": "..."}).
            timeout: Max seconds to wait for the worker's response.

        Returns:
            Dict parsed from the worker's JSON response. On error, returns
            {"success": False, "message": "<error description>"}.
        """
        with self._worker_lock:
            # Lazy start: if worker isn't running, start it now
            if self._worker_process is None or self._worker_process.poll() is not None:
                if self._worker_process is not None:
                    decky.logger.warning(f"{LOG} worker: process died, restarting...")
                    self._stop_worker()

                if not self._start_worker():
                    return {"success": False, "message": "Failed to start GCP worker subprocess"}

            action = command_dict.get("action", "unknown")
            decky.logger.info(f"{LOG} worker: sending command: {action}")

            # Write the command as a JSON line to the worker's stdin
            try:
                self._worker_process.stdin.write(json.dumps(command_dict) + "\n")
                self._worker_process.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                decky.logger.error(f"{LOG} worker: stdin write error: {e}")
                self._stop_worker()
                return {"success": False, "message": f"Worker pipe error: {e}"}

            # Read the response with a timeout.
            # We use a reader thread + join(timeout) because stdout.readline()
            # is blocking and there's no portable way to read with a timeout
            # on a pipe file object.
            response_holder = [None]

            def _read_response():
                try:
                    line = self._worker_process.stdout.readline()
                    if line:
                        response_holder[0] = line.strip()
                except Exception:
                    pass

            reader = threading.Thread(target=_read_response, daemon=True)
            reader.start()
            reader.join(timeout=timeout)

            if reader.is_alive():
                # Timed out — the worker is probably stuck
                decky.logger.error(f"{LOG} worker: response timed out after {timeout}s")
                self._stop_worker()
                return {"success": False, "message": f"Worker response timed out after {timeout}s"}

            raw_response = response_holder[0]
            if not raw_response:
                # Empty response — worker probably crashed
                decky.logger.error(f"{LOG} worker: empty response (worker may have crashed)")
                self._stop_worker()
                return {"success": False, "message": "Worker returned empty response"}

            # Parse the JSON response
            try:
                return json.loads(raw_response)
            except json.JSONDecodeError as e:
                decky.logger.error(f"{LOG} worker: invalid JSON response: {e}")
                decky.logger.error(f"{LOG} worker: raw response: {raw_response[:500]}")
                self._stop_worker()
                return {"success": False, "message": f"Worker JSON parse error: {e}"}

    def _drain_worker_stderr(self):
        """
        Drain the worker's stderr in a daemon thread.

        Reads lines from the worker's stderr pipe and logs them via Decky's
        logger. Without this thread, the stderr pipe buffer would fill up
        (~64KB) and the worker would block trying to write diagnostic logs.

        Runs until the pipe is closed (worker exits) or an error occurs.
        """
        try:
            for line in self._worker_process.stderr:
                stripped = line.strip()
                if stripped:
                    decky.logger.debug(f"{LOG} worker: {stripped}")
        except (ValueError, OSError):
            pass  # Pipe closed — worker exited, normal shutdown

    # =========================================================================
    # Persistent LOCAL worker: _start_local_worker / _stop_local_worker / etc.
    # =========================================================================
    # Same pattern as the GCP worker, but uses bundled Python 3.12 and
    # local_worker.py with RapidOCR + Piper TTS. No credentials needed.

    def _start_local_worker(self):
        """
        Launch the persistent local_worker.py subprocess in serve mode.

        Pre-flight checks: bundled Python 3.12, local_worker.py, and models dir
        must exist. The subprocess reads LOCAL_MODELS_DIR from its environment
        and initializes RapidOCR + Piper models once at startup.

        Returns:
            True if the worker started and signaled ready, False otherwise.
        """
        if not self._local_python_path:
            decky.logger.error(f"{LOG} local worker: bundled Python 3.12 not found — cannot start")
            return False

        if not os.path.exists(self._local_worker_script):
            decky.logger.error(f"{LOG} local worker: local_worker.py not found at {self._local_worker_script}")
            return False

        if not os.path.isdir(self._local_models_dir):
            decky.logger.error(f"{LOG} local worker: models dir not found at {self._local_models_dir}")
            return False

        # Build subprocess environment
        env = os.environ.copy()
        env["PYTHONPATH"] = self._local_py_modules_path
        env["PYTHONNOUSERSITE"] = "1"
        env["LOCAL_MODELS_DIR"] = self._local_models_dir
        env["LOCAL_VOICES_DIR"] = self._voices_dir  # On-demand voice downloads dir
        # Limit CPU threads — leave cores for the game
        env["OMP_NUM_THREADS"] = "2"

        cmd = [self._local_python_path, self._local_worker_script, "serve"]
        decky.logger.info(f"{LOG} local worker: starting persistent subprocess...")

        try:
            self._local_worker_process = subprocess.Popen(
                cmd,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except Exception as e:
            decky.logger.error(f"{LOG} local worker: failed to launch: {e}")
            self._local_worker_process = None
            return False

        # Start stderr drain thread
        self._local_worker_stderr_thread = threading.Thread(
            target=self._drain_local_worker_stderr,
            daemon=True,
            name="dcr_local_worker_stderr",
        )
        self._local_worker_stderr_thread.start()

        # Wait for ready signal (ONNX model loading can take a while first run)
        ready_result = [None]

        def _read_ready():
            try:
                line = self._local_worker_process.stdout.readline()
                if line:
                    ready_result[0] = json.loads(line.strip())
            except Exception as e:
                ready_result[0] = {"ready": False, "message": f"Ready read error: {e}"}

        reader = threading.Thread(target=_read_ready, daemon=True)
        reader.start()
        reader.join(timeout=60)  # 60s — ONNX model loading may be slow first run

        if reader.is_alive():
            decky.logger.error(f"{LOG} local worker: timed out waiting for ready signal (60s)")
            self._stop_local_worker()
            return False

        ready = ready_result[0]
        if ready is None or not ready.get("ready", False):
            msg = ready.get("message", "unknown") if ready else "no response"
            decky.logger.error(f"{LOG} local worker: not ready: {msg}")
            self._stop_local_worker()
            return False

        decky.logger.info(f"{LOG} local worker: persistent worker ready (pid={self._local_worker_process.pid})")
        return True

    def _stop_local_worker(self):
        """
        Stop the persistent local worker subprocess gracefully.
        Same shutdown cascade as _stop_worker(): JSON shutdown → SIGTERM → SIGKILL.
        """
        if self._local_worker_process is None:
            return

        pid = self._local_worker_process.pid

        try:
            if self._local_worker_process.poll() is None:
                try:
                    self._local_worker_process.stdin.write('{"action":"shutdown"}\n')
                    self._local_worker_process.stdin.flush()
                except (BrokenPipeError, OSError):
                    pass

                try:
                    self._local_worker_process.wait(timeout=3)
                    decky.logger.info(f"{LOG} local worker: stopped gracefully (pid={pid})")
                except subprocess.TimeoutExpired:
                    decky.logger.warning(f"{LOG} local worker: graceful shutdown timed out, sending SIGTERM")
                    try:
                        self._local_worker_process.send_signal(signal.SIGTERM)
                        self._local_worker_process.wait(timeout=2)
                        decky.logger.info(f"{LOG} local worker: stopped via SIGTERM (pid={pid})")
                    except subprocess.TimeoutExpired:
                        decky.logger.warning(f"{LOG} local worker: SIGTERM timed out, sending SIGKILL")
                        self._local_worker_process.kill()
                        self._local_worker_process.wait(timeout=2)
                    except ProcessLookupError:
                        pass
            else:
                decky.logger.debug(f"{LOG} local worker: already exited (pid={pid})")

        except ProcessLookupError:
            decky.logger.debug(f"{LOG} local worker: process already gone (pid={pid})")
        except Exception as e:
            decky.logger.error(f"{LOG} local worker: error stopping: {e}")
        finally:
            for pipe in (self._local_worker_process.stdin, self._local_worker_process.stdout, self._local_worker_process.stderr):
                try:
                    if pipe:
                        pipe.close()
                except Exception:
                    pass
            self._local_worker_process = None
            self._local_worker_stderr_thread = None

    def _send_to_local_worker(self, command_dict, timeout=OCR_TTS_TIMEOUT):
        """
        Send a command to the persistent local worker and wait for a response.
        Same pattern as _send_to_worker(): lazy start, thread-safe, auto-restart.
        """
        with self._local_worker_lock:
            # Lazy start: if worker isn't running, start it now
            if self._local_worker_process is None or self._local_worker_process.poll() is not None:
                if self._local_worker_process is not None:
                    decky.logger.warning(f"{LOG} local worker: process died, restarting...")
                    self._stop_local_worker()

                if not self._start_local_worker():
                    return {"success": False, "message": "Failed to start local worker subprocess"}

            action = command_dict.get("action", "unknown")
            decky.logger.info(f"{LOG} local worker: sending command: {action}")

            # Write command
            try:
                self._local_worker_process.stdin.write(json.dumps(command_dict) + "\n")
                self._local_worker_process.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                decky.logger.error(f"{LOG} local worker: stdin write error: {e}")
                self._stop_local_worker()
                return {"success": False, "message": f"Local worker pipe error: {e}"}

            # Read response with timeout
            response_holder = [None]

            def _read_response():
                try:
                    line = self._local_worker_process.stdout.readline()
                    if line:
                        response_holder[0] = line.strip()
                except Exception:
                    pass

            reader = threading.Thread(target=_read_response, daemon=True)
            reader.start()
            reader.join(timeout=timeout)

            if reader.is_alive():
                decky.logger.error(f"{LOG} local worker: response timed out after {timeout}s")
                self._stop_local_worker()
                return {"success": False, "message": f"Local worker response timed out after {timeout}s"}

            raw_response = response_holder[0]
            if not raw_response:
                decky.logger.error(f"{LOG} local worker: empty response (worker may have crashed)")
                self._stop_local_worker()
                return {"success": False, "message": "Local worker returned empty response"}

            try:
                return json.loads(raw_response)
            except json.JSONDecodeError as e:
                decky.logger.error(f"{LOG} local worker: invalid JSON response: {e}")
                decky.logger.error(f"{LOG} local worker: raw response: {raw_response[:500]}")
                self._stop_local_worker()
                return {"success": False, "message": f"Local worker JSON parse error: {e}"}

    def _drain_local_worker_stderr(self):
        """Drain the local worker's stderr in a daemon thread (same as GCP version)."""
        try:
            for line in self._local_worker_process.stderr:
                stripped = line.strip()
                if stripped:
                    decky.logger.debug(f"{LOG} local: {stripped}")
        except (ValueError, OSError):
            pass

    def _send_command(self, command_dict, provider, timeout=OCR_TTS_TIMEOUT):
        """
        Unified routing helper — sends a command to the correct worker based on provider.

        Args:
            command_dict: Dict to send as JSON.
            provider: "gcp" or "local".
            timeout: Max seconds to wait for response.

        Returns:
            Dict parsed from the worker's JSON response.
        """
        if provider == "gcp":
            return self._send_to_worker(command_dict, timeout)
        else:
            return self._send_to_local_worker(command_dict, timeout)

    # =========================================================================
    # Audio playback: _start_playback(), _stop_playback(), _cleanup_tts_temp()
    # =========================================================================
    # Audio playback via ffplay/mpv/pw-play. Unlike worker subprocesses (which
    # use run()), we use Popen here because playback is a long-running background
    # process that the user may want to stop at any time.
    # Supports both MP3 (GCP TTS) and WAV (local Piper TTS) — all players handle both.

    def _start_playback(self, audio_path):
        """
        Start playing an audio file (MP3 or WAV) via the discovered audio player.

        Stops any currently playing audio first. Launches the player as a
        background process (Popen) so it plays asynchronously while the UI
        remains responsive.

        Supported players and their flags:
          - mpv:    --no-video --really-quiet --volume=N <file>
          - ffplay: -nodisp -autoexit -loglevel quiet -volume N <file>
          - pw-play: --volume=F <file>  (F is 0.0-1.0 float)

        Args:
            audio_path: Absolute path to the audio file to play (MP3 or WAV).

        Returns:
            True if playback started successfully, False otherwise.
        """
        # Stop any existing playback before starting new audio
        self._stop_playback()

        if not self._audio_player_path:
            decky.logger.error(f"{LOG} no audio player found — cannot play audio")
            return False

        if not os.path.exists(audio_path):
            decky.logger.error(f"{LOG} audio file not found: {audio_path}")
            return False

        # Get volume from settings (0-100)
        volume = self.settings.get("volume", DEFAULT_SETTINGS["volume"])

        # Build the command based on which player was discovered.
        # Each player has different CLI conventions for volume and quiet mode.
        player = self._audio_player_name
        if player == "mpv":
            cmd = [
                self._audio_player_path,
                "--no-video",          # Audio only, no video window
                "--really-quiet",      # Suppress all output
                f"--volume={volume}",  # Volume 0-100
                audio_path,
            ]
        elif player == "ffplay":
            # ffplay volume is 0-100 (SDL volume), matches our setting range.
            # -autoexit makes ffplay quit when the file ends (otherwise it hangs).
            cmd = [
                self._audio_player_path,
                "-nodisp",             # No video display window
                "-autoexit",           # Exit when playback finishes
                "-loglevel", "quiet",  # Suppress all output
                "-volume", str(volume),
                audio_path,
            ]
        elif player == "pw-play":
            # pw-play volume is a float 0.0-1.0, so convert from 0-100.
            pw_volume = round(volume / 100.0, 2)
            cmd = [
                self._audio_player_path,
                f"--volume={pw_volume}",
                audio_path,
            ]
        else:
            decky.logger.error(f"{LOG} unknown audio player: {player}")
            return False

        try:
            decky.logger.info(f"{LOG} starting playback via {player}: {audio_path} (volume={volume})")

            # The Decky Loader service runs as root, so the audio player
            # can't find the user's PipeWire/PulseAudio session by default.
            # We need to set XDG_RUNTIME_DIR so it can connect to the
            # audio socket at /run/user/1000/pipewire-0 (or pulse/).
            env = os.environ.copy()
            env["XDG_RUNTIME_DIR"] = "/run/user/1000"

            # Launch as a background process. stdout/stderr to DEVNULL
            # since we suppress output via player flags anyway.
            self._playback_process = subprocess.Popen(
                cmd,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            # Track the temp file so _stop_playback() can clean it up
            self._tts_temp_path = audio_path

            decky.logger.info(f"{LOG} playback started (pid={self._playback_process.pid})")

            # Start a daemon thread that waits for the playback process to
            # exit.  This ensures waitpid() is called promptly even when the
            # UI panel is closed and nobody is polling get_playback_status().
            # Without this, ffplay becomes a zombie (Z / <defunct>) after it
            # finishes playing because no code path calls wait() on it.
            proc = self._playback_process  # capture for the closure
            reaper = threading.Thread(
                target=self._reap_playback,
                args=(proc,),
                daemon=True,
                name="dcr-playback-reaper",
            )
            reaper.start()

            return True

        except Exception as e:
            decky.logger.error(f"{LOG} failed to start {player}: {e}")
            decky.logger.error(traceback.format_exc())
            self._playback_process = None
            return False

    def _reap_playback(self, proc):
        """
        Reaper thread target: waits for a playback process to exit, then
        cleans up the temp MP3 file.

        This prevents zombie processes when ffplay finishes naturally and
        nobody is polling from the frontend (e.g., QAM panel is closed).

        If _stop_playback() kills the process first, wait() returns
        immediately and this thread exits — no conflict.

        Args:
            proc: The subprocess.Popen object to wait on.
        """
        try:
            proc.wait()  # blocks until the process exits (reaps the zombie)
            decky.logger.debug(f"{LOG} playback process {proc.pid} reaped (exit={proc.returncode})")
        except Exception as e:
            decky.logger.debug(f"{LOG} reaper error for pid {proc.pid}: {e}")

        # Only clean up if this proc is still the active playback process.
        # If _stop_playback() or a new _start_playback() already replaced it,
        # they own the cleanup — we must not interfere.
        if self._playback_process is proc:
            self._playback_process = None
            self._cleanup_tts_temp()

    def _stop_playback(self):
        """
        Stop any currently playing mpv process and clean up the temp MP3 file.

        Uses a graceful shutdown approach:
          1. SIGTERM — asks mpv to exit cleanly
          2. wait(timeout=2) — give it 2 seconds to stop
          3. SIGKILL — force-kill if it didn't stop (shouldn't happen with mpv)

        Safe to call even if nothing is playing (no-op).
        """
        if self._playback_process is None:
            return

        try:
            # Check if the process is still alive (poll() returns None if running)
            if self._playback_process.poll() is None:
                decky.logger.info(f"{LOG} stopping playback (pid={self._playback_process.pid})")

                # SIGTERM: polite request to exit
                self._playback_process.send_signal(signal.SIGTERM)

                try:
                    # Wait up to 2 seconds for clean exit
                    self._playback_process.wait(timeout=2)
                    decky.logger.info(f"{LOG} playback stopped gracefully")
                except subprocess.TimeoutExpired:
                    # mpv didn't exit in time — force kill
                    decky.logger.warning(f"{LOG} mpv didn't stop in 2s, sending SIGKILL")
                    self._playback_process.kill()
                    self._playback_process.wait(timeout=2)
            else:
                decky.logger.debug(f"{LOG} playback process already exited")

        except ProcessLookupError:
            # Process already exited between our poll() check and signal send.
            # This is a normal race condition, not an error.
            decky.logger.debug(f"{LOG} playback process already gone")

        except Exception as e:
            decky.logger.error(f"{LOG} error stopping playback: {e}")
            decky.logger.error(traceback.format_exc())

        finally:
            self._playback_process = None
            self._cleanup_tts_temp()

    def _cleanup_tts_temp(self):
        """
        Remove the current TTS temp MP3 file if it exists.
        Called after playback stops (naturally or by user request).
        """
        if self._tts_temp_path:
            try:
                if os.path.exists(self._tts_temp_path):
                    os.remove(self._tts_temp_path)
                    decky.logger.debug(f"{LOG} cleaned up TTS temp file: {self._tts_temp_path}")
            except OSError as e:
                decky.logger.warning(f"{LOG} failed to clean up {self._tts_temp_path}: {e}")
            finally:
                self._tts_temp_path = None

    # =========================================================================
    # Interface sounds: _play_interface_sound() (Phase 11)
    # =========================================================================
    # Plays short UI feedback WAV files independently of TTS playback.
    # Unlike _start_playback() which kills existing audio first and tracks the
    # process in self._playback_process, this method spawns a fire-and-forget
    # subprocess with a daemon reaper thread. Multiple interface sounds can
    # overlap with each other AND with TTS playback without interruption.

    def _play_interface_sound(self, sound_name):
        """
        Play a short UI feedback sound (WAV file) without interfering with TTS.

        The sound is played via the same audio player (mpv/ffplay/pw-play)
        discovered at startup. A daemon reaper thread prevents zombie processes.

        Args:
            sound_name: One of the keys in INTERFACE_SOUNDS
                        ("selection_start", "selection_end", "stop").

        Returns:
            True if the sound started playing, False on any error.
        """
        # Respect the mute setting — return early without error
        if self.settings.get("mute_interface_sounds", DEFAULT_SETTINGS["mute_interface_sounds"]):
            decky.logger.debug(f"{LOG} interface sound muted, skipping: {sound_name}")
            return True

        # Map sound name to filename
        filename = INTERFACE_SOUNDS.get(sound_name)
        if not filename:
            decky.logger.error(f"{LOG} unknown interface sound: {sound_name}")
            return False

        # Build the full path to the WAV file in the audio/ directory
        audio_path = os.path.join(decky.DECKY_PLUGIN_DIR, "audio", filename)
        if not os.path.exists(audio_path):
            decky.logger.error(f"{LOG} interface sound file not found: {audio_path}")
            return False

        if not self._audio_player_path:
            decky.logger.error(f"{LOG} no audio player found — cannot play interface sound")
            return False

        # Get volume from settings (0-100)
        volume = self.settings.get("volume", DEFAULT_SETTINGS["volume"])

        # Build the command based on the discovered player (same logic as
        # _start_playback() but without tracking in self._playback_process).
        player = self._audio_player_name
        if player == "mpv":
            cmd = [
                self._audio_player_path,
                "--no-video",
                "--really-quiet",
                f"--volume={volume}",
                audio_path,
            ]
        elif player == "ffplay":
            cmd = [
                self._audio_player_path,
                "-nodisp",
                "-autoexit",
                "-loglevel", "quiet",
                "-volume", str(volume),
                audio_path,
            ]
        elif player == "pw-play":
            pw_volume = round(volume / 100.0, 2)
            cmd = [
                self._audio_player_path,
                f"--volume={pw_volume}",
                audio_path,
            ]
        else:
            decky.logger.error(f"{LOG} unknown audio player: {player}")
            return False

        try:
            decky.logger.info(f"{LOG} playing interface sound: {sound_name} via {player}")

            # Set XDG_RUNTIME_DIR for PipeWire/PulseAudio session access
            # (Decky Loader runs as root, needs this for audio output)
            env = os.environ.copy()
            env["XDG_RUNTIME_DIR"] = "/run/user/1000"

            # Fire-and-forget: NOT tracked in self._playback_process so it
            # doesn't interfere with TTS playback start/stop logic.
            proc = subprocess.Popen(
                cmd,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            # Daemon reaper thread prevents zombie processes after the short
            # sound finishes playing. proc.wait() blocks until exit, then the
            # thread dies automatically (daemon=True).
            reaper = threading.Thread(
                target=proc.wait,
                daemon=True,
                name=f"dcr-sound-reaper-{proc.pid}",
            )
            reaper.start()

            decky.logger.debug(f"{LOG} interface sound started (pid={proc.pid})")
            return True

        except Exception as e:
            decky.logger.error(f"{LOG} failed to play interface sound {sound_name}: {e}")
            decky.logger.error(traceback.format_exc())
            return False

    # =========================================================================
    # OCR pipeline: _perform_ocr_sync() (internal helper)
    # =========================================================================
    # Synchronous method that captures a screenshot and runs OCR on it.
    # Must be run in a thread pool (not on the async event loop) because
    # both _capture_screenshot_sync() and _send_to_worker() are blocking.
    #
    # Pipeline: capture screenshot → write to temp file → send to persistent worker → return result
    def _perform_ocr_sync(self):
        """
        Capture a screenshot and perform OCR on it.
        Routes to GCP or local worker based on the ocr_provider setting.

        Returns:
            Dict with keys: success, text, char_count, line_count, message.
        """
        # Step 1: Determine provider and check prerequisites
        ocr_provider = self.settings.get("ocr_provider", DEFAULT_SETTINGS["ocr_provider"])

        if ocr_provider == "gcp":
            creds_b64 = self.settings.get("gcp_credentials_base64", "")
            if not creds_b64:
                return {
                    "success": False, "text": "", "char_count": 0,
                    "line_count": 0, "message": "GCP credentials not configured",
                }
        else:
            if not self._local_python_path:
                return {
                    "success": False, "text": "", "char_count": 0,
                    "line_count": 0, "message": "Local OCR unavailable (bundled Python not found)",
                }

        # Step 2: Capture a fresh screenshot
        decky.logger.info(f"{LOG} OCR pipeline: capturing screenshot...")
        capture_result = self._capture_screenshot_sync()

        if not capture_result["success"]:
            return {
                "success": False,
                "text": "",
                "char_count": 0,
                "line_count": 0,
                "message": f"Screenshot failed: {capture_result['error']}",
            }

        image_bytes = capture_result["image_bytes"]
        decky.logger.info(f"{LOG} OCR pipeline: screenshot captured ({len(image_bytes):,} bytes)")

        # Step 3: Write the image to a temp file for the subprocess to read.
        fd = None
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(suffix=".png", prefix="dcr_ocr_")
            os.write(fd, image_bytes)
            os.close(fd)
            fd = None

            decky.logger.info(f"{LOG} OCR pipeline: wrote temp image to {tmp_path}")

            # Step 4: Send OCR request to the appropriate worker
            result = self._send_command(
                {"action": "ocr", "image_path": tmp_path},
                provider=ocr_provider,
                timeout=OCR_TIMEOUT,
            )

            # Ensure the result has all expected keys (the worker should always
            # include these, but be defensive)
            return {
                "success": result.get("success", False),
                "text": result.get("text", ""),
                "char_count": result.get("char_count", 0),
                "line_count": result.get("line_count", 0),
                "message": result.get("message", "Unknown error"),
            }

        except Exception as e:
            decky.logger.error(f"{LOG} OCR pipeline error: {e}")
            decky.logger.error(traceback.format_exc())
            return {
                "success": False,
                "text": "",
                "char_count": 0,
                "line_count": 0,
                "message": f"OCR pipeline error: {e}",
            }
        finally:
            # Clean up: close the file descriptor if still open
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            # Clean up: remove the temp file
            if tmp_path:
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                        decky.logger.debug(f"{LOG} cleaned up OCR temp file: {tmp_path}")
                except OSError as e:
                    decky.logger.warning(f"{LOG} failed to clean up {tmp_path}: {e}")

    # =========================================================================
    # RPC: perform_ocr()
    # =========================================================================
    # Async wrapper around _perform_ocr_sync(). Runs the blocking OCR pipeline
    # in a thread pool so it doesn't freeze the event loop.
    #
    # Returns a dict to the frontend:
    #   {success: bool, text: str, char_count: int, line_count: int, message: str}
    #
    # Called from the frontend via:
    #   const performOcr = callable<[], OcrResult>("perform_ocr");
    async def perform_ocr(self):
        decky.logger.info(f"{LOG} perform_ocr() called")

        # Run the blocking OCR pipeline in the thread pool executor.
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(_capture_executor, self._perform_ocr_sync)

        if result["success"]:
            decky.logger.info(f"{LOG} OCR complete: {result['char_count']} chars, {result['line_count']} lines")
        else:
            decky.logger.warning(f"{LOG} OCR failed: {result['message']}")

        return result

    # =========================================================================
    # TTS pipeline: _perform_tts_sync(text) (internal helper)
    # =========================================================================
    # Synchronous method that synthesizes speech from text and starts playback.
    # Must be run in a thread pool because _send_to_worker() and _start_playback()
    # are blocking calls.
    #
    # Pipeline: validate → create temp file → send to persistent worker → start playback
    def _perform_tts_sync(self, text):
        """
        Synthesize speech from text and start playback.
        Routes to GCP or local worker based on the tts_provider setting.

        Args:
            text: The text to convert to speech.

        Returns:
            Dict with keys: success, message, audio_size.
        """
        # Step 1: Determine provider and check prerequisites
        tts_provider = self.settings.get("tts_provider", DEFAULT_SETTINGS["tts_provider"])

        if tts_provider == "gcp":
            creds_b64 = self.settings.get("gcp_credentials_base64", "")
            if not creds_b64:
                return {"success": False, "message": "GCP credentials not configured", "audio_size": 0}
        else:
            if not self._local_python_path:
                return {"success": False, "message": "Local TTS unavailable (bundled Python not found)", "audio_size": 0}

        # Step 2: Validate text
        if not text or not text.strip():
            return {"success": False, "message": "No text to speak", "audio_size": 0}

        # Step 3: Get voice and speech rate settings based on provider
        if tts_provider == "gcp":
            voice_id = self.settings.get("voice_id", DEFAULT_SETTINGS["voice_id"])
            speech_rate = self.settings.get("speech_rate", DEFAULT_SETTINGS["speech_rate"])
        else:
            voice_id = self.settings.get("local_voice_id", DEFAULT_SETTINGS["local_voice_id"])
            speech_rate = self.settings.get("local_speech_rate", DEFAULT_SETTINGS["local_speech_rate"])

            # Auto-download voice if not yet present (on-demand download)
            if not self._is_voice_downloaded(voice_id):
                decky.logger.info(f"{LOG} TTS pipeline: voice '{voice_id}' not downloaded, downloading...")
                dl_result = self._download_voice_sync(voice_id)
                if not dl_result["success"]:
                    return {"success": False, "message": f"Voice download failed: {dl_result['message']}", "audio_size": 0}

        decky.logger.info(f"{LOG} TTS pipeline ({tts_provider}): {len(text):,} chars, voice={voice_id}, rate={speech_rate}")

        # Step 4: Create a temp file for the audio output.
        # GCP produces MP3, local (Piper) produces WAV.
        audio_suffix = ".mp3" if tts_provider == "gcp" else ".wav"
        fd = None
        tmp_path = None
        playback_started = False

        try:
            fd, tmp_path = tempfile.mkstemp(suffix=audio_suffix, prefix="dcr_tts_")
            os.close(fd)
            fd = None

            # Step 5: Send TTS request to the appropriate worker.
            # For multi-speaker Piper voices, include speaker_id from the
            # PIPER_VOICES registry so the worker knows which speaker to use.
            command = {
                "action": "tts",
                "text": text,
                "output_path": tmp_path,
                "speech_rate": speech_rate,
                "voice_id": voice_id,
            }
            voice_meta = PIPER_VOICES.get(voice_id, {})
            if "speaker_id" in voice_meta:
                command["speaker_id"] = voice_meta["speaker_id"]

            result = self._send_command(command, provider=tts_provider, timeout=TTS_TIMEOUT)

            if not result.get("success", False):
                return {
                    "success": False,
                    "message": result.get("message", "TTS failed"),
                    "audio_size": 0,
                }

            # Step 6: Verify the audio file was written and is non-empty
            if not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
                return {
                    "success": False,
                    "message": "TTS produced empty audio file",
                    "audio_size": 0,
                }

            audio_size = result.get("audio_size", os.path.getsize(tmp_path))

            # Step 7: Start playback — if successful, the temp file is now
            # owned by the playback process (cleaned up when playback stops)
            playback_started = self._start_playback(tmp_path)

            if playback_started:
                return {
                    "success": True,
                    "message": f"Playing: {audio_size:,} bytes, voice={voice_id}",
                    "audio_size": audio_size,
                }
            else:
                return {
                    "success": False,
                    "message": "TTS synthesis succeeded but audio playback failed (mpv not found?)",
                    "audio_size": audio_size,
                }

        except Exception as e:
            decky.logger.error(f"{LOG} TTS pipeline error: {e}")
            decky.logger.error(traceback.format_exc())
            return {
                "success": False,
                "message": f"TTS pipeline error: {e}",
                "audio_size": 0,
            }
        finally:
            # Clean up the file descriptor if still open
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            # Clean up the temp file ONLY if playback didn't start.
            # If playback started, _stop_playback() owns the cleanup.
            if not playback_started and tmp_path:
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                        decky.logger.debug(f"{LOG} cleaned up TTS temp file (playback didn't start): {tmp_path}")
                except OSError as e:
                    decky.logger.warning(f"{LOG} failed to clean up {tmp_path}: {e}")

    # =========================================================================
    # Pipeline: _read_screen_sync() (internal helper)
    # =========================================================================
    # Synchronous method that chains capture → OCR+TTS → playback into a
    # pipeline. Sends the combined "ocr_tts" action to the persistent worker.
    # With a warm worker, only the actual GCP API calls are paid — no Python
    # startup, imports, or client init overhead.
    #
    # Cancellation is checked before the capture and before the worker call.
    # Once the worker call starts, it runs to completion (bounded by
    # OCR_TTS_TIMEOUT). The cancel flag is checked again before playback.
    #
    # Provider-aware: routes OCR and TTS to the correct worker based on
    # settings. If both providers are the same, uses the combined ocr_tts
    # action for efficiency. If mixed, runs OCR and TTS sequentially on
    # different workers.
    def _read_screen_sync(self, crop_region=None):
        """
        End-to-end Read Screen pipeline: capture → OCR+TTS → playback.

        Args:
            crop_region: Optional dict {"x1", "y1", "x2", "y2"} to crop the
                        screenshot before OCR. None = full screen (Phase 12).

        Returns:
            Dict with keys: success, message, step, text, audio_size.
            The `text` field is populated even on TTS failure so the frontend
            can display OCR results regardless.
        """
        ocr_tmp_path = None   # Temp file for the screenshot PNG
        tts_tmp_path = None   # Temp file for the synthesized audio
        playback_started = False

        # Timing: track each step to identify bottlenecks
        t_pipeline_start = time.monotonic()

        try:
            # ----- Pre-flight: determine providers and check prerequisites -----
            ocr_provider = self.settings.get("ocr_provider", DEFAULT_SETTINGS["ocr_provider"])
            tts_provider = self.settings.get("tts_provider", DEFAULT_SETTINGS["tts_provider"])

            # Check GCP credentials if any provider needs them
            if ocr_provider == "gcp" or tts_provider == "gcp":
                creds_b64 = self.settings.get("gcp_credentials_base64", "")
                if not creds_b64:
                    return {
                        "success": False,
                        "message": "GCP credentials not configured",
                        "step": "idle", "text": "", "audio_size": 0,
                    }

            # Check local availability if any provider needs it
            if ocr_provider == "local" or tts_provider == "local":
                if not self._local_python_path:
                    return {
                        "success": False,
                        "message": "Local inference unavailable (bundled Python not found)",
                        "step": "idle", "text": "", "audio_size": 0,
                    }

            # ----- Step 1: Capture screenshot -----
            if self._pipeline_cancel.is_set():
                return {"success": False, "message": "Pipeline cancelled", "step": "cancelled", "text": "", "audio_size": 0}

            self._pipeline_step = "capturing"
            t_step = time.monotonic()
            decky.logger.info(f"{LOG} pipeline: capturing screenshot...")
            capture_result = self._capture_screenshot_sync()

            if not capture_result["success"]:
                return {
                    "success": False,
                    "message": f"Capture failed: {capture_result['error']}",
                    "step": "capturing", "text": "", "audio_size": 0,
                }

            image_bytes = capture_result["image_bytes"]
            t_capture = time.monotonic() - t_step
            decky.logger.info(f"{LOG} pipeline: screenshot captured ({len(image_bytes):,} bytes) [{t_capture:.2f}s]")

            # ----- Step 2: OCR + TTS -----
            if self._pipeline_cancel.is_set():
                return {"success": False, "message": "Pipeline cancelled", "step": "cancelled", "text": "", "audio_size": 0}

            self._pipeline_step = "ocr"
            t_step = time.monotonic()

            # Write image to temp file for the worker subprocess
            fd, ocr_tmp_path = tempfile.mkstemp(suffix=".png", prefix="dcr_pipe_ocr_")
            os.write(fd, image_bytes)
            os.close(fd)

            # Create the TTS output temp file path (WAV for local, MP3 for GCP)
            audio_suffix = ".wav" if tts_provider == "local" else ".mp3"
            fd2, tts_tmp_path = tempfile.mkstemp(suffix=audio_suffix, prefix="dcr_pipe_tts_")
            os.close(fd2)

            # Get voice and speech rate settings for the TTS portion
            if tts_provider == "gcp":
                voice_id = self.settings.get("voice_id", DEFAULT_SETTINGS["voice_id"])
                speech_rate = self.settings.get("speech_rate", DEFAULT_SETTINGS["speech_rate"])
            else:
                voice_id = self.settings.get("local_voice_id", DEFAULT_SETTINGS["local_voice_id"])
                speech_rate = self.settings.get("local_speech_rate", DEFAULT_SETTINGS["local_speech_rate"])

                # Auto-download voice if not yet present (on-demand download)
                if not self._is_voice_downloaded(voice_id):
                    if self._pipeline_cancel.is_set():
                        return {"success": False, "message": "Pipeline cancelled", "step": "cancelled", "text": "", "audio_size": 0}

                    self._pipeline_step = "downloading"
                    decky.logger.info(f"{LOG} pipeline: voice '{voice_id}' not downloaded, downloading...")
                    dl_result = self._download_voice_sync(voice_id)
                    if not dl_result["success"]:
                        return {
                            "success": False,
                            "message": f"Voice download failed: {dl_result['message']}",
                            "step": "downloading", "text": "", "audio_size": 0,
                        }
                    decky.logger.info(f"{LOG} pipeline: voice downloaded, continuing...")

            # Route based on whether both providers are the same
            if ocr_provider == tts_provider:
                # Same provider — use combined ocr_tts action for efficiency
                decky.logger.info(f"{LOG} pipeline: running OCR+TTS combined ({ocr_provider})...")
                command = {
                    "action": "ocr_tts",
                    "image_path": ocr_tmp_path,
                    "output_path": tts_tmp_path,
                    "speech_rate": speech_rate,
                    "voice_id": voice_id,
                }
                # Phase 12: pass crop_region to worker if specified
                if crop_region:
                    command["crop_region"] = crop_region
                voice_meta = PIPER_VOICES.get(voice_id, {})
                if "speaker_id" in voice_meta:
                    command["speaker_id"] = voice_meta["speaker_id"]

                result = self._send_command(command, provider=ocr_provider, timeout=OCR_TTS_TIMEOUT)
                t_ocr_tts = time.monotonic() - t_step

                if not result.get("success", False):
                    return {
                        "success": False,
                        "message": f"OCR+TTS failed: {result.get('message', 'Unknown error')}",
                        "step": "ocr", "text": "", "audio_size": 0,
                    }

                ocr_text = result.get("text", "")
                char_count = result.get("char_count", len(ocr_text))
                audio_size = result.get("audio_size", 0)

            else:
                # Mixed providers — run OCR first, then TTS separately
                decky.logger.info(f"{LOG} pipeline: running OCR ({ocr_provider}) then TTS ({tts_provider})...")

                # OCR step — include crop_region if specified (Phase 12)
                ocr_cmd = {"action": "ocr", "image_path": ocr_tmp_path}
                if crop_region:
                    ocr_cmd["crop_region"] = crop_region
                ocr_result = self._send_command(
                    ocr_cmd,
                    provider=ocr_provider, timeout=OCR_TIMEOUT,
                )

                if not ocr_result.get("success", False):
                    return {
                        "success": False,
                        "message": f"OCR failed: {ocr_result.get('message', 'Unknown error')}",
                        "step": "ocr", "text": "", "audio_size": 0,
                    }

                ocr_text = ocr_result.get("text", "")
                char_count = ocr_result.get("char_count", len(ocr_text))

                if not ocr_text.strip():
                    t_ocr_tts = time.monotonic() - t_step
                    decky.logger.info(f"{LOG} pipeline: no text detected [{t_ocr_tts:.2f}s]")
                    return {
                        "success": False, "message": "No text detected on screen",
                        "step": "ocr", "text": "", "audio_size": 0,
                    }

                # Check cancellation before TTS
                if self._pipeline_cancel.is_set():
                    return {"success": False, "message": "Pipeline cancelled", "step": "cancelled", "text": ocr_text, "audio_size": 0}

                self._pipeline_step = "tts"

                # TTS step
                tts_command = {
                    "action": "tts",
                    "text": ocr_text,
                    "output_path": tts_tmp_path,
                    "speech_rate": speech_rate,
                    "voice_id": voice_id,
                }
                voice_meta = PIPER_VOICES.get(voice_id, {})
                if "speaker_id" in voice_meta:
                    tts_command["speaker_id"] = voice_meta["speaker_id"]

                tts_result = self._send_command(tts_command, provider=tts_provider, timeout=TTS_TIMEOUT)
                t_ocr_tts = time.monotonic() - t_step

                if not tts_result.get("success", False):
                    return {
                        "success": False,
                        "message": f"TTS failed: {tts_result.get('message', 'Unknown error')}",
                        "step": "tts", "text": ocr_text, "audio_size": 0,
                    }

                audio_size = tts_result.get("audio_size", 0)

            # No text detected — the subprocess returned success with empty text
            if not ocr_text.strip():
                decky.logger.info(f"{LOG} pipeline: no text detected [{t_ocr_tts:.2f}s]")
                return {
                    "success": False,
                    "message": "No text detected on screen",
                    "step": "ocr", "text": "", "audio_size": 0,
                }

            decky.logger.info(
                f"{LOG} pipeline: OCR+TTS complete: {char_count} chars, "
                f"{audio_size:,} bytes [{t_ocr_tts:.2f}s]"
            )

            # Verify audio file exists and is non-empty
            if not os.path.exists(tts_tmp_path) or os.path.getsize(tts_tmp_path) == 0:
                return {
                    "success": False,
                    "message": "TTS produced empty audio file",
                    "step": "tts", "text": ocr_text, "audio_size": 0,
                }

            # ----- Step 3: Playback -----
            if self._pipeline_cancel.is_set():
                return {"success": False, "message": "Pipeline cancelled", "step": "cancelled", "text": ocr_text, "audio_size": audio_size}

            self._pipeline_step = "playing"
            playback_started = self._start_playback(tts_tmp_path)

            t_total = time.monotonic() - t_pipeline_start
            if playback_started:
                decky.logger.info(
                    f"{LOG} pipeline: complete [{t_total:.2f}s total] "
                    f"(capture={t_capture:.2f}s, ocr_tts={t_ocr_tts:.2f}s)"
                )
                return {
                    "success": True,
                    "message": f"Playing: {char_count} chars, {audio_size:,} bytes ({t_total:.1f}s)",
                    "step": "playing", "text": ocr_text, "audio_size": audio_size,
                }
            else:
                return {
                    "success": False,
                    "message": "Audio playback failed to start (no audio player found?)",
                    "step": "playing", "text": ocr_text, "audio_size": audio_size,
                }

        except Exception as e:
            decky.logger.error(f"{LOG} pipeline error: {e}")
            decky.logger.error(traceback.format_exc())
            return {
                "success": False,
                "message": f"Pipeline error: {e}",
                "step": "error",
                "text": "",
                "audio_size": 0,
            }
        finally:
            # Always clean up the OCR temp file (screenshot PNG)
            if ocr_tmp_path:
                try:
                    if os.path.exists(ocr_tmp_path):
                        os.remove(ocr_tmp_path)
                        decky.logger.debug(f"{LOG} pipeline: cleaned up OCR temp file: {ocr_tmp_path}")
                except OSError as e:
                    decky.logger.warning(f"{LOG} pipeline: failed to clean up {ocr_tmp_path}: {e}")

            # Clean up TTS temp file ONLY if playback didn't start.
            # If playback started, _stop_playback() owns the cleanup.
            if not playback_started and tts_tmp_path:
                try:
                    if os.path.exists(tts_tmp_path):
                        os.remove(tts_tmp_path)
                        decky.logger.debug(f"{LOG} pipeline: cleaned up TTS temp file: {tts_tmp_path}")
                except OSError as e:
                    decky.logger.warning(f"{LOG} pipeline: failed to clean up {tts_tmp_path}: {e}")

            # Reset pipeline state
            self._pipeline_step = "idle"
            self._pipeline_running = False

    # =========================================================================
    # Phase 12: _read_screen_with_crop() — async pipeline wrapper with crop
    # =========================================================================
    # Same guards as read_screen(), but accepts an optional crop_region dict.
    # Used by capture mode handlers (button trigger, swipe, two-tap).

    async def _read_screen_with_crop(self, crop_region=None):
        """
        Async entry point for the pipeline with optional image cropping.

        Args:
            crop_region: Optional dict {"x1", "y1", "x2", "y2"} to crop the
                        screenshot before OCR. None = full screen.

        Returns:
            Pipeline result dict (same as read_screen).
        """
        decky.logger.info(f"{LOG} _read_screen_with_crop(crop={crop_region})")

        # Reject if a pipeline is already running
        if self._pipeline_running:
            decky.logger.warning(f"{LOG} pipeline already running — rejecting")
            return {
                "success": False,
                "message": "Pipeline already running",
                "step": self._pipeline_step,
                "text": "",
                "audio_size": 0,
            }

        # Initialize pipeline state
        self._pipeline_cancel.clear()
        self._pipeline_running = True
        self._pipeline_step = "starting"

        # Run the blocking pipeline in the thread pool executor
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            _capture_executor,
            lambda: self._read_screen_sync(crop_region=crop_region),
        )

        if result["success"]:
            decky.logger.info(f"{LOG} pipeline complete: {result['message']}")
        else:
            decky.logger.warning(f"{LOG} pipeline ended: {result['message']}")

        return result

    # =========================================================================
    # RPC: read_screen()
    # =========================================================================
    # Async entry point for the end-to-end pipeline. Rejects concurrent runs,
    # clears the cancellation flag, and dispatches to _read_screen_sync() in
    # the thread pool executor.
    #
    # Called from the frontend via:
    #   const readScreen = callable<[], ReadScreenResult>("read_screen");
    async def read_screen(self):
        """RPC: End-to-end pipeline (full screen, no crop). Delegates to _read_screen_with_crop."""
        decky.logger.info(f"{LOG} read_screen() called")
        return await self._read_screen_with_crop(crop_region=None)

    # =========================================================================
    # RPC: stop_pipeline()
    # =========================================================================
    # Sets the cancellation flag (checked between pipeline steps) and stops
    # audio playback immediately if currently playing.
    #
    # Note: this does NOT kill in-flight GCP worker subprocesses. They will
    # complete naturally within their timeout (60s for combined OCR+TTS). The
    # cancel flag is checked after the subprocess returns.
    #
    # Called from the frontend via:
    #   const stopPipeline = callable<[], StopPipelineResult>("stop_pipeline");
    async def stop_pipeline(self):
        decky.logger.info(f"{LOG} stop_pipeline() called")
        self._pipeline_cancel.set()
        self._stop_playback()
        return {
            "success": True,
            "message": "Pipeline stop requested",
        }

    # =========================================================================
    # RPC: get_pipeline_status()
    # =========================================================================
    # Lightweight poll target for the frontend. Returns the current pipeline
    # step and whether audio is playing. No subprocess involved.
    #
    # Called from the frontend via:
    #   const getPipelineStatus = callable<[], PipelineStatus>("get_pipeline_status");
    async def get_pipeline_status(self):
        is_playing = (
            self._playback_process is not None
            and self._playback_process.poll() is None
        )
        return {
            "running": self._pipeline_running,
            "step": self._pipeline_step,
            "is_playing": is_playing,
        }

    # =========================================================================
    # RPC: perform_tts(text)
    # =========================================================================
    # Async wrapper around _perform_tts_sync(). Runs the blocking TTS pipeline
    # in a thread pool so it doesn't freeze the event loop.
    #
    # Returns a dict to the frontend:
    #   {success: bool, message: str, audio_size: int}
    #
    # Called from the frontend via:
    #   const performTts = callable<[string], TtsResult>("perform_tts");
    async def perform_tts(self, text):
        decky.logger.info(f"{LOG} perform_tts() called ({len(text):,} chars)")

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            _capture_executor,
            lambda: self._perform_tts_sync(text),
        )

        if result["success"]:
            decky.logger.info(f"{LOG} TTS complete: {result['message']}")
        else:
            decky.logger.warning(f"{LOG} TTS failed: {result['message']}")

        return result

    # =========================================================================
    # RPC: stop_playback()
    # =========================================================================
    # Stops the current audio playback (if any). Safe to call if nothing is
    # playing — it's a no-op.
    #
    # Called from the frontend via:
    #   const stopPlayback = callable<[], StopResult>("stop_playback");
    async def stop_playback(self):
        decky.logger.info(f"{LOG} stop_playback() called")
        self._stop_playback()
        return {
            "success": True,
            "message": "Playback stopped",
        }

    # =========================================================================
    # RPC: get_playback_status()
    # =========================================================================
    # Lightweight status check — tells the frontend whether audio is currently
    # playing. No subprocess involved, just checks self._playback_process.poll().
    #
    # Called from the frontend via:
    #   const getPlaybackStatus = callable<[], PlaybackStatus>("get_playback_status");
    async def get_playback_status(self):
        is_playing = (
            self._playback_process is not None
            and self._playback_process.poll() is None
        )
        return {"is_playing": is_playing}

    # =========================================================================
    # RPC: play_interface_sound() (Phase 11)
    # =========================================================================
    # Plays a short UI feedback sound. Used by the frontend test buttons and
    # will be called by capture mode logic in Phase 12. Respects the
    # mute_interface_sounds setting.
    #
    # Called from the frontend via:
    #   const playInterfaceSound = callable<[string], {success: boolean; error?: string}>("play_interface_sound");
    async def play_interface_sound(self, sound_name):
        """
        RPC: Play a UI feedback sound (for test buttons and capture modes).
        Respects the mute_interface_sounds setting.

        Args:
            sound_name: One of "selection_start", "selection_end", "stop".

        Returns:
            Dict with "success" (bool) and optional "error" (str).
        """
        decky.logger.debug(f"{LOG} play_interface_sound({sound_name}) called")

        # Run in executor because _play_interface_sound() does subprocess I/O
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(
                _capture_executor,
                self._play_interface_sound,
                sound_name,
            )
            if result:
                return {"success": True}
            else:
                return {"success": False, "error": f"Failed to play sound: {sound_name}"}
        except Exception as e:
            decky.logger.error(f"{LOG} play_interface_sound error: {e}")
            return {"success": False, "error": str(e)}

    # =========================================================================
    # RPC: get_settings()
    # =========================================================================
    # Returns the current settings merged with defaults, plus a computed
    # `is_configured` boolean that tells the frontend whether GCP credentials
    # have been loaded.
    #
    # Called from the frontend via:
    #   const getSettings = callable<[], PluginSettings>("get_settings");
    async def get_settings(self):
        decky.logger.debug(f"{LOG} get_settings() called")

        # Start with defaults, then overlay saved values. This ensures the
        # frontend always gets every expected key, even if the settings file
        # is from an older version that's missing new keys.
        result = dict(DEFAULT_SETTINGS)
        result.update(self.settings.get_all())

        # Compute whether GCP credentials are configured
        creds_b64 = result.get("gcp_credentials_base64", "")
        result["is_gcp_configured"] = bool(creds_b64)

        # Compute whether local inference is available (bundled Python found)
        result["is_local_available"] = self._local_python_path is not None

        # Backwards-compatible is_configured: true if the current providers
        # have everything they need to function
        ocr_provider = result.get("ocr_provider", "local")
        tts_provider = result.get("tts_provider", "local")
        needs_gcp = ocr_provider == "gcp" or tts_provider == "gcp"
        needs_local = ocr_provider == "local" or tts_provider == "local"
        result["is_configured"] = (
            (not needs_gcp or bool(creds_b64))
            and (not needs_local or self._local_python_path is not None)
        )

        # If credentials are stored, decode them to extract the project_id
        # for display in the UI.
        result["project_id"] = ""
        if creds_b64:
            try:
                creds_json = json.loads(base64.b64decode(creds_b64))
                result["project_id"] = creds_json.get("project_id", "")
            except Exception:
                pass

        # Remove the raw credentials from the response — the frontend doesn't
        # need the actual key material, just whether it's configured.
        result.pop("gcp_credentials_base64", None)

        return result

    # =========================================================================
    # RPC: save_setting(key, value)
    # =========================================================================
    # Persist a single setting. Called when the user toggles a switch or
    # changes a dropdown in the UI.
    #
    # Called from the frontend via:
    #   const saveSetting = callable<[string, any], boolean>("save_setting");
    async def save_setting(self, key, value):
        decky.logger.info(f"{LOG} save_setting({key}, {value})")

        # Don't allow the frontend to directly set credentials — that goes
        # through load_credentials_file() instead.
        if key == "gcp_credentials_base64":
            decky.logger.warning(f"{LOG} direct credential setting not allowed")
            return False

        result = self.settings.set(key, value)

        # Handle trigger button changes — start/stop/reconfigure the monitor
        if key == "trigger_button":
            if value == "disabled":
                # Stop the monitor entirely
                if self._hidraw_monitor:
                    self._hidraw_monitor.stop()
                    self._hidraw_monitor = None
                    decky.logger.info(f"{LOG} button monitor stopped (trigger disabled)")
            else:
                if self._hidraw_monitor:
                    # Monitor already running — just change the target button
                    self._hidraw_monitor.configure(target_button=value)
                else:
                    # Monitor not running — create and start it
                    hold_time_ms = self.settings.get("hold_time_ms", DEFAULT_SETTINGS["hold_time_ms"])
                    self._hidraw_monitor = HidrawButtonMonitor(
                        target_button=value,
                        hold_threshold_ms=hold_time_ms,
                        on_trigger=self._on_button_trigger,
                        logger=decky.logger,
                        log_prefix=LOG,
                    )
                    started = self._hidraw_monitor.start()
                    if started:
                        decky.logger.info(f"{LOG} button monitor started: button={value}")
                    else:
                        decky.logger.warning(f"{LOG} button monitor failed to start")

        elif key == "hold_time_ms":
            # Update hold threshold on the running monitor (no restart needed)
            if self._hidraw_monitor:
                self._hidraw_monitor.configure(hold_threshold_ms=value)

        elif key == "debug":
            # Sync the Python logger level to the debug toggle.
            # This takes effect immediately — no restart needed.
            if value:
                decky.logger.setLevel(logging.DEBUG)
                decky.logger.info(f"{LOG} debug logging enabled")
            else:
                decky.logger.info(f"{LOG} debug logging disabled")
                decky.logger.setLevel(logging.INFO)

        elif key == "capture_mode":
            # Phase 12: sync touchscreen monitor for the new capture mode
            # and reset any in-progress two-tap selection state.
            self._sync_touchscreen_for_mode(value)
            if self._two_tap_timer:
                self._two_tap_timer.cancel()
                self._two_tap_timer = None
            self._capture_state = "idle"
            decky.logger.info(f"{LOG} capture mode changed to {value}")

        elif key == "enabled":
            # When the master switch is toggled, actively manage background
            # resources. Disabling stops any running pipeline, kills audio
            # playback, and shuts down both worker subprocesses (freeing
            # memory, gRPC connections, and ONNX models). Also stops the
            # touchscreen monitor. Enabling is a no-op — workers lazy-start
            # on the next request.
            if not value:
                decky.logger.info(f"{LOG} plugin disabled — stopping background activity")
                self._pipeline_cancel.set()
                self._stop_playback()
                self._stop_worker()
                self._stop_local_worker()
                # Stop touchscreen monitor when disabled
                if self._touchscreen_monitor:
                    self._touchscreen_monitor.stop()
                    self._touchscreen_monitor = None
                    decky.logger.info(f"{LOG} touchscreen monitor stopped (plugin disabled)")
                # Reset capture state
                if self._two_tap_timer:
                    self._two_tap_timer.cancel()
                    self._two_tap_timer = None
                self._capture_state = "idle"
            else:
                decky.logger.info(f"{LOG} plugin enabled")
                # Re-sync touchscreen monitor on re-enable
                self._sync_touchscreen_for_mode()

        elif key == "ocr_provider":
            # Provider changed — stop the old provider's worker so it doesn't
            # linger. The new provider's worker will lazy-start on next use.
            old_provider = self.settings.get("ocr_provider", DEFAULT_SETTINGS["ocr_provider"])
            if old_provider != value:
                if old_provider == "gcp":
                    decky.logger.info(f"{LOG} OCR provider changed to {value}, stopping GCP worker")
                    self._stop_worker()
                else:
                    decky.logger.info(f"{LOG} OCR provider changed to {value}, stopping local worker")
                    self._stop_local_worker()

        elif key == "tts_provider":
            old_provider = self.settings.get("tts_provider", DEFAULT_SETTINGS["tts_provider"])
            if old_provider != value:
                if old_provider == "gcp":
                    decky.logger.info(f"{LOG} TTS provider changed to {value}, stopping GCP worker")
                    self._stop_worker()
                else:
                    decky.logger.info(f"{LOG} TTS provider changed to {value}, stopping local worker")
                    self._stop_local_worker()

        return result

    # =========================================================================
    # RPC: list_directory(path)
    # =========================================================================
    # Lists the contents of a directory on the Steam Deck's filesystem.
    # Used by the file browser UI to let the user navigate to their GCP
    # service account JSON file.
    #
    # Returns a dict with:
    #   path:    The absolute path that was listed (normalized)
    #   entries: List of {name, is_dir, size} dicts, sorted dirs-first
    #   error:   Error message string, or null if successful
    #
    # Called from the frontend via:
    #   const listDirectory = callable<[string], DirectoryListing>("list_directory");
    async def list_directory(self, path):
        decky.logger.debug(f"{LOG} list_directory({path})")

        try:
            # Normalize the path to resolve things like "/home/deck/../deck"
            path = os.path.realpath(path)

            # Read the directory contents
            raw_entries = os.listdir(path)

            entries = []
            for name in raw_entries:
                # Skip hidden files/directories (start with .) to reduce clutter
                if name.startswith("."):
                    continue

                full_path = os.path.join(path, name)
                is_dir = os.path.isdir(full_path)

                # For files, only show .json files (that's all we need for
                # credential loading). Directories are always shown so the
                # user can navigate.
                if not is_dir and not name.lower().endswith(".json"):
                    continue

                # Get file size (0 for directories)
                try:
                    size = os.path.getsize(full_path) if not is_dir else 0
                except OSError:
                    size = 0

                entries.append({
                    "name": name,
                    "is_dir": is_dir,
                    "size": size,
                })

            # Sort: directories first (alphabetical), then files (alphabetical).
            # This makes the file browser easier to navigate.
            entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))

            return {
                "path": path,
                "entries": entries,
                "error": None,
            }

        except PermissionError:
            decky.logger.warning(f"{LOG} permission denied: {path}")
            return {
                "path": path,
                "entries": [],
                "error": f"Permission denied: {path}",
            }
        except FileNotFoundError:
            decky.logger.warning(f"{LOG} directory not found: {path}")
            return {
                "path": path,
                "entries": [],
                "error": f"Directory not found: {path}",
            }
        except Exception as e:
            decky.logger.error(f"{LOG} list_directory error: {e}")
            decky.logger.error(traceback.format_exc())
            return {
                "path": path,
                "entries": [],
                "error": str(e),
            }

    # =========================================================================
    # RPC: load_credentials_file(file_path)
    # =========================================================================
    # Reads a GCP service account JSON file from disk, validates it has the
    # required fields, base64-encodes it, and stores it in settings.
    #
    # Returns a dict with:
    #   valid:      True if the file was a valid service account JSON
    #   message:    Human-readable success/error message
    #   project_id: The GCP project ID from the file (empty on error)
    #
    # Called from the frontend via:
    #   const loadCredentialsFile = callable<[string], CredentialResult>("load_credentials_file");
    async def load_credentials_file(self, file_path):
        decky.logger.info(f"{LOG} load_credentials_file({file_path})")

        try:
            # Step 1: Read the file
            with open(file_path, "r") as f:
                raw_content = f.read()

            # Step 2: Parse as JSON
            try:
                creds = json.loads(raw_content)
            except json.JSONDecodeError as e:
                return {
                    "valid": False,
                    "message": f"Invalid JSON: {e}",
                    "project_id": "",
                }

            # Step 3: Validate required GCP service account fields.
            # A valid service account JSON must have all of these fields.
            missing_fields = [
                field for field in REQUIRED_GCP_FIELDS if field not in creds
            ]
            if missing_fields:
                return {
                    "valid": False,
                    "message": f"Missing required fields: {', '.join(missing_fields)}",
                    "project_id": "",
                }

            # Step 4: Verify the "type" field is "service_account"
            if creds.get("type") != "service_account":
                return {
                    "valid": False,
                    "message": f"Expected type 'service_account', got '{creds.get('type')}'",
                    "project_id": "",
                }

            # Step 5: Base64-encode the JSON and store it in settings.
            # We encode the raw file content (not our parsed version) to
            # preserve the exact original format.
            encoded = base64.b64encode(raw_content.encode("utf-8")).decode("utf-8")
            self.settings.set("gcp_credentials_base64", encoded)

            # Stop the persistent worker so it restarts with the new
            # credentials on the next request. Without this, the worker
            # would keep using the old credentials until plugin reload.
            self._stop_worker()
            decky.logger.info(f"{LOG} worker stopped for credential refresh")

            project_id = creds.get("project_id", "unknown")
            decky.logger.info(f"{LOG} credentials loaded for project: {project_id}")

            return {
                "valid": True,
                "message": f"Credentials loaded! Project: {project_id}",
                "project_id": project_id,
            }

        except FileNotFoundError:
            return {
                "valid": False,
                "message": f"File not found: {file_path}",
                "project_id": "",
            }
        except PermissionError:
            return {
                "valid": False,
                "message": f"Permission denied: {file_path}",
                "project_id": "",
            }
        except Exception as e:
            decky.logger.error(f"{LOG} load_credentials_file error: {e}")
            decky.logger.error(traceback.format_exc())
            return {
                "valid": False,
                "message": f"Error: {e}",
                "project_id": "",
            }

    # =========================================================================
    # RPC: clear_credentials()
    # =========================================================================
    # Removes stored GCP credentials from settings.
    #
    # Called from the frontend via:
    #   const clearCredentials = callable<[], boolean>("clear_credentials");
    async def clear_credentials(self):
        decky.logger.info(f"{LOG} clear_credentials() called")
        # Stop the persistent worker before clearing credentials —
        # it holds initialized clients with the old credentials.
        self._stop_worker()
        return self.settings.set("gcp_credentials_base64", "")

    # =========================================================================
    # RPC: get_button_monitor_status()
    # =========================================================================
    # Returns the current state of the hidraw button monitor for the UI
    # status indicator. Lightweight — no subprocess or I/O involved.
    #
    # Called from the frontend via:
    #   const getButtonMonitorStatus = callable<[], ButtonMonitorStatus>("get_button_monitor_status");
    async def get_button_monitor_status(self):
        decky.logger.debug(f"{LOG} get_button_monitor_status() called")

        trigger_button = self.settings.get("trigger_button", DEFAULT_SETTINGS["trigger_button"])

        if trigger_button == "disabled":
            return {
                "running": False,
                "initialized": False,
                "device_path": None,
                "error_count": 0,
                "target_button": "disabled",
                "hold_threshold_ms": self.settings.get("hold_time_ms", DEFAULT_SETTINGS["hold_time_ms"]),
            }

        if self._hidraw_monitor:
            return self._hidraw_monitor.get_status()

        # Monitor should be running but isn't (failed to start)
        return {
            "running": False,
            "initialized": False,
            "device_path": None,
            "error_count": 0,
            "target_button": trigger_button,
            "hold_threshold_ms": self.settings.get("hold_time_ms", DEFAULT_SETTINGS["hold_time_ms"]),
        }

    # =========================================================================
    # RPC: get_touchscreen_status()
    # =========================================================================
    # Returns the current state of the touchscreen monitor for the UI status
    # indicator. Lightweight — no subprocess or I/O involved.
    #
    # Called from the frontend via:
    #   const getTouchscreenStatus = callable<[], TouchscreenStatus>("get_touchscreen_status");
    async def get_touchscreen_status(self):
        decky.logger.debug(f"{LOG} get_touchscreen_status() called")

        if self._touchscreen_monitor:
            return self._touchscreen_monitor.get_status()

        # Monitor not running — return empty status
        return {
            "running": False,
            "initialized": False,
            "device_path": None,
            "error_count": 0,
            "physical_max_x": 0,
            "physical_max_y": 0,
            "last_touch": None,
        }

    # =========================================================================
    # RPC: get_last_touch()
    # =========================================================================
    # Returns the coordinates of the last detected tap. Lightweight poll target.
    #
    # Called from the frontend via:
    #   const getLastTouch = callable<[], {x: number, y: number} | null>("get_last_touch");
    async def get_last_touch(self):
        if self._touchscreen_monitor:
            return self._touchscreen_monitor.get_last_touch()
        return None

    # =========================================================================
    # RPC: apply_last_selection_to_fixed_region() (Phase 12)
    # =========================================================================
    # Copies the last_selection_* coordinates (set by swipe/two-tap modes)
    # into the fixed_region_* coordinates. Gives the user a quick way to
    # lock in a region they selected interactively.
    #
    # Called from the frontend via:
    #   const applyLastSelectionToFixedRegion = callable<[], {success: boolean; message: string}>(
    #     "apply_last_selection_to_fixed_region");
    async def apply_last_selection_to_fixed_region(self):
        decky.logger.info(f"{LOG} apply_last_selection_to_fixed_region() called")
        x1 = self.settings.get("last_selection_x1", DEFAULT_SETTINGS["last_selection_x1"])
        y1 = self.settings.get("last_selection_y1", DEFAULT_SETTINGS["last_selection_y1"])
        x2 = self.settings.get("last_selection_x2", DEFAULT_SETTINGS["last_selection_x2"])
        y2 = self.settings.get("last_selection_y2", DEFAULT_SETTINGS["last_selection_y2"])

        self.settings.set("fixed_region_x1", x1)
        self.settings.set("fixed_region_y1", y1)
        self.settings.set("fixed_region_x2", x2)
        self.settings.set("fixed_region_y2", y2)

        decky.logger.info(f"{LOG} fixed region set to ({x1},{y1})-({x2},{y2})")
        return {
            "success": True,
            "message": f"Fixed region set to ({x1},{y1})-({x2},{y2})",
        }

    # =========================================================================
    # RPC: get_available_voices()
    # =========================================================================
    # Returns the PIPER_VOICES registry enriched with download status for each
    # voice. The frontend uses this to populate the voice dropdown with
    # indicators showing which voices are downloaded and which need downloading.
    #
    # Called from the frontend via:
    #   const getAvailableVoices = callable<[], VoiceRegistry>("get_available_voices");
    async def get_available_voices(self):
        decky.logger.debug(f"{LOG} get_available_voices() called")

        voices = {}
        for voice_id, info in PIPER_VOICES.items():
            onnx_path = os.path.join(self._voices_dir, f"{voice_id}.onnx")
            downloaded = os.path.exists(onnx_path)
            file_size = 0
            if downloaded:
                try:
                    file_size = os.path.getsize(onnx_path)
                except OSError:
                    pass
            voices[voice_id] = {
                "label": info["label"],
                "language": info["language"],
                "speakers": info["speakers"],
                "downloaded": downloaded,
                "file_size": file_size,
            }

        return voices

    # =========================================================================
    # RPC: download_voice(voice_id)
    # =========================================================================
    # Downloads a Piper voice model (.onnx + .onnx.json) from HuggingFace.
    # Downloads to .tmp files first, then renames on success to avoid partial
    # files from interrupted downloads.
    #
    # Called from the frontend via:
    #   const downloadVoice = callable<[string], DownloadResult>("download_voice");
    async def download_voice(self, voice_id):
        decky.logger.info(f"{LOG} download_voice({voice_id}) called")

        if voice_id not in PIPER_VOICES:
            return {"success": False, "message": f"Unknown voice: {voice_id}", "file_size": 0}

        # Run the blocking download in the thread pool
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            _capture_executor,
            lambda: self._download_voice_sync(voice_id),
        )
        return result

    def _download_voice_sync(self, voice_id):
        """
        Download a Piper voice model (.onnx + .onnx.json) from HuggingFace.

        Uses curl for reliable HTTPS/redirect handling. Downloads to .tmp files
        first, then renames on success (atomic-ish, prevents partial files).

        Args:
            voice_id: The voice identifier (e.g., "en_US-amy-medium").

        Returns:
            Dict with keys: success, message, file_size.
        """
        onnx_url = _piper_voice_url(voice_id, ".onnx")
        json_url = _piper_voice_url(voice_id, ".onnx.json")

        onnx_path = os.path.join(self._voices_dir, f"{voice_id}.onnx")
        json_path = os.path.join(self._voices_dir, f"{voice_id}.onnx.json")
        onnx_tmp = onnx_path + ".tmp"
        json_tmp = json_path + ".tmp"

        # Build a clean environment for curl. Decky Loader is a PyInstaller
        # bundle that sets LD_LIBRARY_PATH to its temp dir (e.g., /tmp/_MEI*/)
        # containing an older libssl.so.3. System curl links against the
        # system's newer OpenSSL, so inheriting PyInstaller's LD_LIBRARY_PATH
        # causes "OPENSSL_3.2.0 not found" errors. Stripping these vars lets
        # curl use the system's native libraries.
        curl_env = os.environ.copy()
        curl_env.pop("LD_LIBRARY_PATH", None)
        curl_env.pop("LD_PRELOAD", None)

        try:
            # Download .onnx.json first (small file, quick validation)
            decky.logger.info(f"{LOG} downloading voice config: {json_url}")
            result = subprocess.run(
                ["curl", "-L", "-f", "-o", json_tmp, json_url],
                capture_output=True, text=True,
                timeout=VOICE_DOWNLOAD_TIMEOUT,
                env=curl_env,
            )
            if result.returncode != 0:
                decky.logger.error(f"{LOG} voice config download failed: {result.stderr[:200]}")
                return {"success": False, "message": "Voice config download failed — check internet connection", "file_size": 0}

            # Download .onnx model (large file, ~63MB)
            decky.logger.info(f"{LOG} downloading voice model: {onnx_url}")
            result = subprocess.run(
                ["curl", "-L", "-f", "-o", onnx_tmp, onnx_url],
                capture_output=True, text=True,
                timeout=VOICE_DOWNLOAD_TIMEOUT,
                env=curl_env,
            )
            if result.returncode != 0:
                decky.logger.error(f"{LOG} voice model download failed: {result.stderr[:200]}")
                return {"success": False, "message": "Voice model download failed — check internet connection", "file_size": 0}

            # Validate downloaded files exist and aren't empty
            if not os.path.exists(onnx_tmp) or os.path.getsize(onnx_tmp) == 0:
                return {"success": False, "message": "Downloaded model file is empty", "file_size": 0}
            if not os.path.exists(json_tmp) or os.path.getsize(json_tmp) == 0:
                return {"success": False, "message": "Downloaded config file is empty", "file_size": 0}

            # Rename from .tmp to final paths (atomic-ish on same filesystem)
            os.rename(json_tmp, json_path)
            os.rename(onnx_tmp, onnx_path)

            file_size = os.path.getsize(onnx_path)
            decky.logger.info(f"{LOG} voice downloaded: {voice_id} ({file_size:,} bytes)")

            return {
                "success": True,
                "message": f"Voice downloaded: {PIPER_VOICES[voice_id]['label']}",
                "file_size": file_size,
            }

        except subprocess.TimeoutExpired:
            decky.logger.error(f"{LOG} voice download timed out after {VOICE_DOWNLOAD_TIMEOUT}s")
            return {"success": False, "message": "Download timed out — check internet connection", "file_size": 0}

        except Exception as e:
            decky.logger.error(f"{LOG} voice download error: {e}")
            decky.logger.error(traceback.format_exc())
            return {"success": False, "message": f"Download error: {e}", "file_size": 0}

        finally:
            # Clean up temp files on failure
            for tmp in (onnx_tmp, json_tmp):
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except OSError:
                    pass

    # =========================================================================
    # RPC: delete_voice(voice_id)
    # =========================================================================
    # Removes a downloaded Piper voice model. If the deleted voice is currently
    # selected, stops the local worker so its in-memory cache is cleared.
    #
    # Called from the frontend via:
    #   const deleteVoice = callable<[string], DeleteResult>("delete_voice");
    async def delete_voice(self, voice_id):
        decky.logger.info(f"{LOG} delete_voice({voice_id}) called")

        onnx_path = os.path.join(self._voices_dir, f"{voice_id}.onnx")
        json_path = os.path.join(self._voices_dir, f"{voice_id}.onnx.json")

        deleted = False
        for path in (onnx_path, json_path):
            try:
                if os.path.exists(path):
                    os.remove(path)
                    deleted = True
                    decky.logger.debug(f"{LOG} deleted: {path}")
            except OSError as e:
                decky.logger.error(f"{LOG} failed to delete {path}: {e}")
                return {"success": False, "message": f"Failed to delete voice: {e}"}

        # If the deleted voice is the currently selected one, stop the local
        # worker so its in-memory voice cache is cleared. Next TTS call will
        # restart the worker and load a different voice (or trigger re-download).
        current_voice = self.settings.get("local_voice_id", DEFAULT_SETTINGS["local_voice_id"])
        if current_voice == voice_id:
            decky.logger.info(f"{LOG} deleted current voice — stopping local worker to clear cache")
            self._stop_local_worker()

        if deleted:
            return {"success": True, "message": f"Voice deleted: {voice_id}"}
        else:
            return {"success": True, "message": f"Voice not found: {voice_id}"}

    def _is_voice_downloaded(self, voice_id):
        """
        Quick check whether a voice model file exists in the voices directory.

        Args:
            voice_id: The voice identifier (e.g., "en_US-amy-medium").

        Returns:
            True if the .onnx file exists, False otherwise.
        """
        onnx_path = os.path.join(self._voices_dir, f"{voice_id}.onnx")
        return os.path.exists(onnx_path)

