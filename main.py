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
import traceback
import subprocess
import shutil
import tempfile
import asyncio
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

        decky.logger.info(f"{LOG} settings initialized")

    # =========================================================================
    # Lifecycle: _unload()
    # =========================================================================
    # Called when the plugin is stopped (e.g., Decky Loader restarts, or the
    # user disables the plugin). The plugin is NOT removed from disk.
    async def _unload(self):
        # Shut down the thread pool executor. wait=False so we don't block
        # the unload if a capture is somehow still running.
        _capture_executor.shutdown(wait=False)
        decky.logger.info(f"{LOG} capture executor shut down")
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

