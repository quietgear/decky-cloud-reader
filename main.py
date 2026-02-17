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
import glob
import traceback
import subprocess
import signal
import shutil
import tempfile
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import decky

# Log prefix — makes our messages easy to find in the Decky Loader journal.
# Usage: decky.logger.info(f"{LOG} message here")
# In the journal, lines will look like: "[DCR] backend loaded"
# Filter with: journalctl -u plugin_loader -f | grep DCR
LOG = "[DCR]"

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

# Thread pool for running blocking subprocess calls (like gst-launch-1.0)
# without blocking the async event loop. Only 1 worker needed since we
# only ever capture one screenshot at a time.
_capture_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dcr_capture")


# =============================================================================
# Default settings — used when no settings file exists yet, and to backfill
# any new keys added in future versions. Each key corresponds to a user-facing
# or internal configuration value.
# =============================================================================

DEFAULT_SETTINGS = {
    # Base64-encoded GCP service account JSON. Stored internally after the user
    # selects a JSON file via the file browser. Never shown to the user directly.
    "gcp_credentials_base64": "",

    # Text-to-Speech voice ID (used in Phase 5). Format: "languageCode-Name".
    "voice_id": "en-US-Neural2-C",

    # TTS speech rate preset (used in Phase 5). One of: x-slow, slow, medium, fast, x-fast.
    "speech_rate": "medium",

    # TTS volume level 0-100 (used in Phase 5).
    "volume": 100,

    # Master on/off switch. When False, L4 button trigger does nothing.
    "enabled": True,

    # When True, extra diagnostic info is logged (useful for troubleshooting).
    "debug": False,
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

        decky.logger.info(f"{LOG} settings initialized")

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

    # =========================================================================
    # Lifecycle: _unload()
    # =========================================================================
    # Called when the plugin is stopped (e.g., Decky Loader restarts, or the
    # user disables the plugin). The plugin is NOT removed from disk.
    async def _unload(self):
        # Step 0: Cancel any running pipeline so it stops between steps
        self._pipeline_cancel.set()
        decky.logger.info(f"{LOG} pipeline cancel flag set")

        # Step 1: Stop any running audio playback and clean up its temp file
        self._stop_playback()
        decky.logger.info(f"{LOG} playback stopped")

        # Step 2: Shut down the thread pool executor. wait=False so we don't
        # block the unload if a capture is somehow still running.
        _capture_executor.shutdown(wait=False)
        decky.logger.info(f"{LOG} capture executor shut down")

        # Step 3: Sweep any orphaned temp files from previous runs.
        # These could exist if the plugin crashed mid-pipeline.
        for pattern in ["/tmp/dcr_*.png", "/tmp/dcr_*.mp3"]:
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
    # Subprocess helper: _run_gcp_worker(action, args_list, timeout)
    # =========================================================================
    # Runs gcp_worker.py as a subprocess under system Python. This is the
    # generic launcher used by OCR (and later TTS in Phase 5).
    #
    # The subprocess gets:
    #   - PYTHONPATH pointing to py_modules/ (so it can import google-cloud libs)
    #   - PYTHONNOUSERSITE=1 (ignore user site-packages to avoid conflicts)
    #   - GCP_CREDENTIALS_BASE64 via env var (not CLI args — avoids ps exposure)
    #
    # Returns a dict parsed from the subprocess's JSON stdout output.
    # On any error (timeout, missing Python, bad JSON), returns {success: False, message: "..."}.
    def _run_gcp_worker(self, action, args_list, timeout=OCR_TIMEOUT):
        """
        Run gcp_worker.py with the given action and arguments.

        Args:
            action: The action to perform (e.g., "ocr", "tts").
            args_list: List of additional CLI arguments (e.g., ["/tmp/image.png"]).
            timeout: Maximum time to wait for the subprocess (seconds).

        Returns:
            Dict parsed from the subprocess's JSON stdout. On error, returns
            {"success": False, "message": "<error description>"}.
        """
        # Pre-flight checks: make sure we have the pieces we need
        if not self._system_python:
            return {"success": False, "message": "System Python not found — cannot run GCP worker"}

        if not os.path.exists(self._gcp_worker_path):
            return {"success": False, "message": f"gcp_worker.py not found at {self._gcp_worker_path}"}

        # Build the command: [/usr/bin/python3, /path/to/gcp_worker.py, action, ...args]
        cmd = [self._system_python, self._gcp_worker_path, action] + args_list

        # Build the environment for the subprocess
        env = os.environ.copy()
        # Tell Python where to find google-cloud packages (installed in py_modules/)
        env["PYTHONPATH"] = self._py_modules_path
        # Prevent system Python from loading user-installed packages that might conflict
        env["PYTHONNOUSERSITE"] = "1"
        # Pass GCP credentials securely via environment variable
        creds_b64 = self.settings.get("gcp_credentials_base64", "")
        if creds_b64:
            env["GCP_CREDENTIALS_BASE64"] = creds_b64

        decky.logger.info(f"{LOG} running gcp_worker: {action} {' '.join(args_list)}")
        decky.logger.debug(f"{LOG} gcp_worker command: {' '.join(cmd)}")

        try:
            # subprocess.run() is used (not Popen) because this is a
            # request-response call: we send input, wait for output, done.
            # run() automatically waits for the process to exit, so no zombies.
            result = subprocess.run(
                cmd,
                env=env,
                capture_output=True,  # Capture both stdout (JSON) and stderr (logs)
                text=True,            # Decode output as UTF-8 strings
                timeout=timeout,
            )

            # Log stderr from the worker (diagnostic messages, not errors necessarily)
            if result.stderr:
                for line in result.stderr.strip().split("\n"):
                    decky.logger.debug(f"{LOG} worker: {line}")

            # Parse the JSON result from stdout
            if not result.stdout.strip():
                decky.logger.error(f"{LOG} gcp_worker produced no stdout (exit code {result.returncode})")
                return {"success": False, "message": "GCP worker produced no output"}

            try:
                parsed = json.loads(result.stdout.strip())
                return parsed
            except json.JSONDecodeError as e:
                decky.logger.error(f"{LOG} gcp_worker output not valid JSON: {e}")
                decky.logger.error(f"{LOG} raw stdout: {result.stdout[:500]}")
                return {"success": False, "message": f"GCP worker output parse error: {e}"}

        except subprocess.TimeoutExpired:
            decky.logger.error(f"{LOG} gcp_worker timed out after {timeout}s")
            return {"success": False, "message": f"GCP worker timed out after {timeout} seconds"}

        except FileNotFoundError:
            decky.logger.error(f"{LOG} system Python not found at {self._system_python}")
            return {"success": False, "message": f"System Python not found at {self._system_python}"}

        except Exception as e:
            decky.logger.error(f"{LOG} gcp_worker error: {e}")
            decky.logger.error(traceback.format_exc())
            return {"success": False, "message": f"GCP worker error: {e}"}

    # =========================================================================
    # Audio playback: _start_playback(), _stop_playback(), _cleanup_tts_temp()
    # =========================================================================
    # mpv is used for audio playback because it's pre-installed on Steam Deck,
    # lightweight, and supports MP3. Unlike gcp_worker.py (which uses run()),
    # we use Popen here because playback is a long-running background process
    # that the user may want to stop at any time.

    def _start_playback(self, mp3_path):
        """
        Start playing an MP3 file via the discovered audio player.

        Stops any currently playing audio first. Launches the player as a
        background process (Popen) so it plays asynchronously while the UI
        remains responsive.

        Supported players and their flags:
          - mpv:    --no-video --really-quiet --volume=N <file>
          - ffplay: -nodisp -autoexit -loglevel quiet -volume N <file>
          - pw-play: --volume=F <file>  (F is 0.0-1.0 float)

        Args:
            mp3_path: Absolute path to the MP3 file to play.

        Returns:
            True if playback started successfully, False otherwise.
        """
        # Stop any existing playback before starting new audio
        self._stop_playback()

        if not self._audio_player_path:
            decky.logger.error(f"{LOG} no audio player found — cannot play audio")
            return False

        if not os.path.exists(mp3_path):
            decky.logger.error(f"{LOG} MP3 file not found: {mp3_path}")
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
                mp3_path,
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
                mp3_path,
            ]
        elif player == "pw-play":
            # pw-play volume is a float 0.0-1.0, so convert from 0-100.
            pw_volume = round(volume / 100.0, 2)
            cmd = [
                self._audio_player_path,
                f"--volume={pw_volume}",
                mp3_path,
            ]
        else:
            decky.logger.error(f"{LOG} unknown audio player: {player}")
            return False

        try:
            decky.logger.info(f"{LOG} starting playback via {player}: {mp3_path} (volume={volume})")

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
            self._tts_temp_path = mp3_path

            decky.logger.info(f"{LOG} playback started (pid={self._playback_process.pid})")
            return True

        except Exception as e:
            decky.logger.error(f"{LOG} failed to start {player}: {e}")
            decky.logger.error(traceback.format_exc())
            self._playback_process = None
            return False

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
    # OCR pipeline: _perform_ocr_sync() (internal helper)
    # =========================================================================
    # Synchronous method that captures a screenshot and runs OCR on it.
    # Must be run in a thread pool (not on the async event loop) because
    # both _capture_screenshot_sync() and _run_gcp_worker() are blocking.
    #
    # Pipeline: capture screenshot → write to temp file → run gcp_worker OCR → return result
    def _perform_ocr_sync(self):
        """
        Capture a screenshot and perform OCR on it.

        Returns:
            Dict with keys: success, text, char_count, line_count, message.
        """
        # Step 1: Check that credentials are configured
        creds_b64 = self.settings.get("gcp_credentials_base64", "")
        if not creds_b64:
            return {
                "success": False,
                "text": "",
                "char_count": 0,
                "line_count": 0,
                "message": "GCP credentials not configured",
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
        # We use a temp file because passing large binary data via stdin/pipe
        # is more complex and error-prone than a file path.
        fd = None
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(suffix=".png", prefix="dcr_ocr_")
            # Write image bytes to the temp file using the file descriptor
            os.write(fd, image_bytes)
            os.close(fd)
            fd = None  # Mark as closed so finally doesn't double-close

            decky.logger.info(f"{LOG} OCR pipeline: wrote temp image to {tmp_path}")

            # Step 4: Run the GCP worker subprocess to perform OCR
            result = self._run_gcp_worker("ocr", [tmp_path])

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
    # Must be run in a thread pool because _run_gcp_worker() and _start_playback()
    # are blocking calls.
    #
    # Pipeline: validate → create temp file → gcp_worker TTS → start mpv playback
    def _perform_tts_sync(self, text):
        """
        Synthesize speech from text and start playback.

        Args:
            text: The text to convert to speech.

        Returns:
            Dict with keys: success, message, audio_size.
        """
        # Step 1: Check that credentials are configured
        creds_b64 = self.settings.get("gcp_credentials_base64", "")
        if not creds_b64:
            return {
                "success": False,
                "message": "GCP credentials not configured",
                "audio_size": 0,
            }

        # Step 2: Validate text
        if not text or not text.strip():
            return {
                "success": False,
                "message": "No text to speak",
                "audio_size": 0,
            }

        # Step 3: Get voice and speech rate settings
        voice_id = self.settings.get("voice_id", DEFAULT_SETTINGS["voice_id"])
        speech_rate = self.settings.get("speech_rate", DEFAULT_SETTINGS["speech_rate"])

        decky.logger.info(f"{LOG} TTS pipeline: {len(text):,} chars, voice={voice_id}, rate={speech_rate}")

        # Step 4: Create a temp file for the MP3 output.
        # mkstemp creates a file with a unique name that won't collide.
        fd = None
        tmp_path = None
        playback_started = False

        try:
            fd, tmp_path = tempfile.mkstemp(suffix=".mp3", prefix="dcr_tts_")
            os.close(fd)
            fd = None  # Mark as closed so finally doesn't double-close

            # Step 5: Run the GCP worker to synthesize speech
            result = self._run_gcp_worker(
                "tts",
                [text, tmp_path, voice_id, speech_rate],
                timeout=TTS_TIMEOUT,
            )

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
    # Synchronous method that chains capture → OCR → TTS → playback into a
    # single pipeline. Each step checks the cancellation flag before proceeding.
    # Cancellation is "between steps" only — once a subprocess starts, it runs
    # to completion (bounded by OCR_TIMEOUT / TTS_TIMEOUT).
    #
    # This method does NOT call _perform_ocr_sync() or _perform_tts_sync()
    # because we need per-step progress tracking and cancellation checks
    # between sub-steps that those combined methods don't support.
    def _read_screen_sync(self):
        """
        End-to-end Read Screen pipeline: capture → OCR → TTS → playback.

        Returns:
            Dict with keys: success, message, step, text, audio_size.
            The `text` field is populated even on TTS failure so the frontend
            can display OCR results regardless.
        """
        ocr_tmp_path = None   # Temp file for the screenshot PNG
        tts_tmp_path = None   # Temp file for the synthesized MP3
        playback_started = False

        try:
            # ----- Pre-flight: check credentials -----
            creds_b64 = self.settings.get("gcp_credentials_base64", "")
            if not creds_b64:
                return {
                    "success": False,
                    "message": "GCP credentials not configured",
                    "step": "idle",
                    "text": "",
                    "audio_size": 0,
                }

            # ----- Step 1: Capture screenshot -----
            if self._pipeline_cancel.is_set():
                return {"success": False, "message": "Pipeline cancelled", "step": "cancelled", "text": "", "audio_size": 0}

            self._pipeline_step = "capturing"
            decky.logger.info(f"{LOG} pipeline: capturing screenshot...")
            capture_result = self._capture_screenshot_sync()

            if not capture_result["success"]:
                return {
                    "success": False,
                    "message": f"Capture failed: {capture_result['error']}",
                    "step": "capturing",
                    "text": "",
                    "audio_size": 0,
                }

            image_bytes = capture_result["image_bytes"]
            decky.logger.info(f"{LOG} pipeline: screenshot captured ({len(image_bytes):,} bytes)")

            # ----- Step 2: OCR -----
            if self._pipeline_cancel.is_set():
                return {"success": False, "message": "Pipeline cancelled", "step": "cancelled", "text": "", "audio_size": 0}

            self._pipeline_step = "ocr"
            decky.logger.info(f"{LOG} pipeline: running OCR...")

            # Write image to temp file for the GCP worker subprocess
            fd, ocr_tmp_path = tempfile.mkstemp(suffix=".png", prefix="dcr_pipe_ocr_")
            os.write(fd, image_bytes)
            os.close(fd)

            ocr_result = self._run_gcp_worker("ocr", [ocr_tmp_path])

            if not ocr_result.get("success", False):
                return {
                    "success": False,
                    "message": f"OCR failed: {ocr_result.get('message', 'Unknown error')}",
                    "step": "ocr",
                    "text": "",
                    "audio_size": 0,
                }

            ocr_text = ocr_result.get("text", "")
            if not ocr_text.strip():
                return {
                    "success": False,
                    "message": "No text detected on screen",
                    "step": "ocr",
                    "text": "",
                    "audio_size": 0,
                }

            char_count = ocr_result.get("char_count", len(ocr_text))
            decky.logger.info(f"{LOG} pipeline: OCR detected {char_count} chars")

            # ----- Step 3: TTS -----
            if self._pipeline_cancel.is_set():
                return {"success": False, "message": "Pipeline cancelled", "step": "cancelled", "text": ocr_text, "audio_size": 0}

            self._pipeline_step = "tts"
            decky.logger.info(f"{LOG} pipeline: synthesizing speech...")

            voice_id = self.settings.get("voice_id", DEFAULT_SETTINGS["voice_id"])
            speech_rate = self.settings.get("speech_rate", DEFAULT_SETTINGS["speech_rate"])

            fd, tts_tmp_path = tempfile.mkstemp(suffix=".mp3", prefix="dcr_pipe_tts_")
            os.close(fd)

            tts_result = self._run_gcp_worker(
                "tts",
                [ocr_text, tts_tmp_path, voice_id, speech_rate],
                timeout=TTS_TIMEOUT,
            )

            if not tts_result.get("success", False):
                return {
                    "success": False,
                    "message": f"TTS failed: {tts_result.get('message', 'Unknown error')}",
                    "step": "tts",
                    "text": ocr_text,
                    "audio_size": 0,
                }

            # Verify audio file exists and is non-empty
            if not os.path.exists(tts_tmp_path) or os.path.getsize(tts_tmp_path) == 0:
                return {
                    "success": False,
                    "message": "TTS produced empty audio file",
                    "step": "tts",
                    "text": ocr_text,
                    "audio_size": 0,
                }

            audio_size = tts_result.get("audio_size", os.path.getsize(tts_tmp_path))
            decky.logger.info(f"{LOG} pipeline: TTS synthesized {audio_size:,} bytes")

            # ----- Step 4: Playback -----
            if self._pipeline_cancel.is_set():
                return {"success": False, "message": "Pipeline cancelled", "step": "cancelled", "text": ocr_text, "audio_size": audio_size}

            self._pipeline_step = "playing"
            playback_started = self._start_playback(tts_tmp_path)

            if playback_started:
                decky.logger.info(f"{LOG} pipeline: playback started")
                return {
                    "success": True,
                    "message": f"Playing: {char_count} chars, {audio_size:,} bytes",
                    "step": "playing",
                    "text": ocr_text,
                    "audio_size": audio_size,
                }
            else:
                return {
                    "success": False,
                    "message": "Audio playback failed to start (no audio player found?)",
                    "step": "playing",
                    "text": ocr_text,
                    "audio_size": audio_size,
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
    # RPC: read_screen()
    # =========================================================================
    # Async entry point for the end-to-end pipeline. Rejects concurrent runs,
    # clears the cancellation flag, and dispatches to _read_screen_sync() in
    # the thread pool executor.
    #
    # Called from the frontend via:
    #   const readScreen = callable<[], ReadScreenResult>("read_screen");
    async def read_screen(self):
        decky.logger.info(f"{LOG} read_screen() called")

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
        result = await loop.run_in_executor(_capture_executor, self._read_screen_sync)

        if result["success"]:
            decky.logger.info(f"{LOG} pipeline complete: {result['message']}")
        else:
            decky.logger.warning(f"{LOG} pipeline ended: {result['message']}")

        return result

    # =========================================================================
    # RPC: stop_pipeline()
    # =========================================================================
    # Sets the cancellation flag (checked between pipeline steps) and stops
    # audio playback immediately if currently playing.
    #
    # Note: this does NOT kill in-flight GCP worker subprocesses. They will
    # complete naturally within their timeouts (45s OCR / 30s TTS). The cancel
    # flag is checked after each subprocess returns.
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

        # Compute whether credentials are configured. The frontend uses this
        # to show "Configured" vs "Not Configured" status.
        creds_b64 = result.get("gcp_credentials_base64", "")
        result["is_configured"] = bool(creds_b64)

        # If credentials are stored, decode them to extract the project_id
        # for display in the UI. We don't send the full credentials to the
        # frontend — just the project_id.
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

        return self.settings.set(key, value)

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
        return self.settings.set("gcp_credentials_base64", "")

