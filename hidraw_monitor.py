# =============================================================================
# Decky Cloud Reader — Hidraw Button Monitor
# =============================================================================
#
# Self-contained module for monitoring Steam Deck controller buttons via the
# Linux hidraw interface. Adapted from Decky-Translator's HidrawButtonMonitor
# with key additions:
#
#   - Hold-time detection: fires a callback only after the target button is
#     held for a configurable duration (e.g., 500ms). This prevents accidental
#     triggers from quick taps.
#
#   - Runtime reconfiguration: target button and hold threshold can be changed
#     while the monitor is running (thread-safe via lock).
#
#   - Callback-based: instead of an event queue, fires a single callback when
#     the hold threshold is reached. The callback is invoked from the monitor
#     thread — the caller is responsible for dispatching to the event loop.
#
# How it works:
#
#   The Steam Deck controller sends 64-byte HID packets at ~250Hz via
#   /dev/hidraw. Each packet contains two 32-bit button state bitmasks:
#
#     Bytes 8-11  (uint32 LE):  "ButtonsL" — face buttons, bumpers, triggers, etc.
#     Bytes 12-15 (uint32 LE):  "ButtonsH" — back buttons (L4/R4), QAM, etc.
#
#   The monitor reads these packets in a background daemon thread, checks if
#   the configured target button is pressed, tracks how long it's been held,
#   and fires the callback once the hold threshold is met.
#
# Device discovery:
#
#   The Steam Deck controller exposes 3 hidraw interfaces. We need interface
#   :1.2 (the gamepad interface). Discovery scans /sys/class/hidraw/ for
#   Valve VID (0x28DE) / PID (0x1205) and prefers the :1.2 interface.
#
# IMPORTANT: This module must NOT import `decky` at module level. Decky's
# logger is passed in via the constructor so this module can be tested
# independently if needed.
# =============================================================================

import fcntl
import os
import select
import struct
import threading
import time

# =============================================================================
# Button mask definitions
# =============================================================================
# These define which bit in the 32-bit button state corresponds to which
# physical button on the Steam Deck controller.

# ButtonsL: bytes 8-11 of the HID packet (uint32 little-endian)
BUTTONS_L = {
    "R2": 0x00000001,
    "L2": 0x00000002,
    "R1": 0x00000004,
    "L1": 0x00000008,
    "Y": 0x00000010,
    "B": 0x00000020,
    "X": 0x00000040,
    "A": 0x00000080,
    "DPAD_UP": 0x00000100,
    "DPAD_RIGHT": 0x00000200,
    "DPAD_LEFT": 0x00000400,
    "DPAD_DOWN": 0x00000800,
    "SELECT": 0x00001000,
    "STEAM": 0x00002000,
    "START": 0x00004000,
    "L5": 0x00008000,
    "R5": 0x00010000,
    "LEFT_PAD_TOUCH": 0x00020000,
    "RIGHT_PAD_TOUCH": 0x00040000,
    "LEFT_PAD_CLICK": 0x00080000,
    "RIGHT_PAD_CLICK": 0x00100000,
    "L3": 0x00400000,
    "R3": 0x04000000,
}

# ButtonsH: bytes 12-15 of the HID packet (uint32 little-endian)
BUTTONS_H = {
    "L4": 0x00000200,
    "R4": 0x00000400,
    "QAM": 0x00040000,
}

# Combine both dicts for easy lookup — given a button name, find which
# group it belongs to (so we know which bytes to check in the packet).
ALL_BUTTONS = {}
for name, mask in BUTTONS_L.items():
    ALL_BUTTONS[name] = ("L", mask)
for name, mask in BUTTONS_H.items():
    ALL_BUTTONS[name] = ("H", mask)

# Buttons that can be used as triggers (the back grip buttons)
TRIGGER_BUTTONS = ["L4", "R4", "L5", "R5"]


# =============================================================================
# HidrawButtonMonitor class
# =============================================================================


class HidrawButtonMonitor:
    """
    Monitors Steam Deck controller via /dev/hidraw for hold-to-trigger
    button detection.

    Usage:
        monitor = HidrawButtonMonitor(
            target_button="L4",
            hold_threshold_ms=500,
            on_trigger=my_callback,
            logger=decky.logger,
            log_prefix="[DCR]",
        )
        monitor.start()      # Starts background thread
        monitor.configure(target_button="R4")  # Change at runtime
        monitor.stop()        # Stops thread, closes device

    The on_trigger callback is called from the monitor thread. If you need
    to run async code, use asyncio.run_coroutine_threadsafe() in the callback.
    """

    # HID packet size for Steam Deck controller
    PACKET_SIZE = 64

    # HID ioctl command for sending feature reports.
    # This is the HIDIOCSFEATURE ioctl — it sends a SET_REPORT request
    # to the HID device. The size parameter (64) is encoded in the ioctl number.
    # Formula: _IOC(IOC_WRITE|IOC_READ, 'H', 0x06, size)
    @staticmethod
    def _hidiocsfeature(size):
        return 0xC0000000 | (size << 16) | (ord("H") << 8) | 0x06

    # HID command IDs for controller initialization
    ID_CLEAR_DIGITAL_MAPPINGS = 0x81
    ID_SET_SETTINGS_VALUES = 0x87
    SETTING_LEFT_TRACKPAD_MODE = 0x07
    SETTING_RIGHT_TRACKPAD_MODE = 0x08
    TRACKPAD_NONE = 0x07
    SETTING_STEAM_WATCHDOG_ENABLE = 0x2D

    def __init__(self, target_button="L4", hold_threshold_ms=500, on_trigger=None, logger=None, log_prefix="[DCR]"):
        """
        Initialize the button monitor.

        Args:
            target_button: Which button to monitor ("L4", "R4", "L5", "R5").
            hold_threshold_ms: How long the button must be held (milliseconds)
                               before the trigger fires. Range: 100-5000ms.
            on_trigger: Callable invoked (from monitor thread) when the hold
                        threshold is reached. Called at most once per press.
            logger: Logger object with .info(), .debug(), .warning(), .error()
                    methods. If None, logging is silently skipped.
            log_prefix: String prefix for log messages (e.g., "[DCR]").
        """
        # Configuration (protected by self._lock)
        self._target_button = target_button
        self._hold_threshold_s = hold_threshold_ms / 1000.0
        self._on_trigger = on_trigger

        # Logging
        self._logger = logger
        self._log_prefix = log_prefix

        # Device state
        self._device_fd = None
        self._device_path = None
        self._initialized = False

        # Thread state
        self._running = False
        self._thread = None
        self._lock = threading.Lock()

        # Button state (only accessed from monitor thread, no lock needed)
        self._last_buttons_l = 0
        self._last_buttons_h = 0
        self._current_buttons = set()
        self._error_count = 0

        # Hold detection state (only accessed from monitor thread)
        self._button_press_start = None  # monotonic timestamp when target button was first pressed
        self._triggered = False  # True if we already fired the callback for this press
        self._cooldown_until = 0.0  # monotonic timestamp — ignore triggers until this time

        # Cooldown duration after a successful trigger (seconds).
        # Prevents rapid re-triggering if the user holds the button too long.
        self._cooldown_duration = 2.0

    # -------------------------------------------------------------------------
    # Logging helpers
    # -------------------------------------------------------------------------

    def _log_info(self, msg):
        if self._logger:
            self._logger.info(f"{self._log_prefix} {msg}")

    def _log_debug(self, msg):
        if self._logger:
            self._logger.debug(f"{self._log_prefix} {msg}")

    def _log_warning(self, msg):
        if self._logger:
            self._logger.warning(f"{self._log_prefix} {msg}")

    def _log_error(self, msg):
        if self._logger:
            self._logger.error(f"{self._log_prefix} {msg}")

    # -------------------------------------------------------------------------
    # Device discovery
    # -------------------------------------------------------------------------

    def _find_device(self):
        """
        Find the Steam Deck controller's gamepad hidraw device.

        Scans /sys/class/hidraw/ for devices matching Valve VID (0x28DE) and
        Steam Deck PID (0x1205). Prefers interface :1.2 (the gamepad interface).

        Returns:
            Device path string (e.g., "/dev/hidraw2") or None if not found.
        """
        candidates = []

        # Scan hidraw0 through hidraw9 for Valve controller devices
        for i in range(10):
            path = f"/dev/hidraw{i}"
            if not os.path.exists(path):
                continue

            uevent_path = f"/sys/class/hidraw/hidraw{i}/device/uevent"
            try:
                with open(uevent_path, "r") as f:
                    content = f.read().upper()
                    # Check for Valve VID (28DE) and Steam Deck PID (1205)
                    if "28DE" in content and "1205" in content:
                        candidates.append((i, path))
                        self._log_debug(f"hidraw: found Valve controller candidate at {path}")
            except Exception as e:
                self._log_debug(f"hidraw: cannot read uevent for hidraw{i}: {e}")

        if not candidates:
            self._log_warning("hidraw: Steam Deck controller not found")
            return None

        # Prefer the :1.2 interface (the gamepad interface that has button data)
        for i, path in candidates:
            try:
                link_target = os.readlink(f"/sys/class/hidraw/hidraw{i}")
                if ":1.2/" in link_target:
                    self._log_info(f"hidraw: found gamepad interface at {path} (interface 1.2)")
                    return path
            except Exception as e:
                self._log_debug(f"hidraw: cannot read symlink for hidraw{i}: {e}")

        # Fallback: try each candidate with a non-blocking read to find data
        for i, path in candidates:
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                try:
                    readable, _, _ = select.select([fd], [], [], 0.1)
                    if readable:
                        os.read(fd, 64)
                        os.close(fd)
                        self._log_info(f"hidraw: found controller at {path} (has data)")
                        return path
                    os.close(fd)
                except Exception:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            except Exception as e:
                self._log_debug(f"hidraw: cannot open {path}: {e}")

        # Last resort: use the highest-numbered candidate
        if candidates:
            path = candidates[-1][1]
            self._log_info(f"hidraw: using controller at {path} (last candidate)")
            return path

        return None

    # -------------------------------------------------------------------------
    # HID initialization
    # -------------------------------------------------------------------------

    def _send_feature_report(self, data):
        """
        Send a HID feature report to the controller.

        Feature reports are used to configure the controller's mode. We send
        commands to disable "lizard mode" (where the controller emulates a
        mouse/keyboard) and enable raw button reporting.

        Args:
            data: List of bytes to send as the feature report payload.

        Returns:
            True if the report was sent successfully, False otherwise.
        """
        if self._device_fd is None:
            return False
        try:
            # Pad the data to 64 bytes (the controller's report size)
            buf = bytes(data) + bytes(64 - len(data))
            fcntl.ioctl(self._device_fd, self._hidiocsfeature(64), buf)
            return True
        except Exception as e:
            self._log_error(f"hidraw: failed to send feature report: {e}")
            return False

    def _initialize_device(self):
        """
        Open the hidraw device and send initialization commands.

        Initialization disables "lizard mode" (where the Steam Deck controller
        emulates a desktop mouse/keyboard) and enables raw gamepad reporting.
        This is required to see L4/R4/L5/R5 button events.

        Returns:
            True if the device was opened and initialized, False otherwise.
        """
        if self._device_path is None:
            self._device_path = self._find_device()
            if self._device_path is None:
                return False

        try:
            # Open with read/write access (write needed for feature reports)
            self._device_fd = os.open(self._device_path, os.O_RDWR)
            self._log_info(f"hidraw: opened {self._device_path}")

            # Command 1: Clear digital mappings — disables "lizard mode" where
            # the controller emulates a mouse/keyboard for desktop use.
            if not self._send_feature_report([self.ID_CLEAR_DIGITAL_MAPPINGS]):
                self._log_warning("hidraw: failed to clear digital mappings")

            # Command 2: Configure trackpad and watchdog settings.
            # - Set both trackpads to "none" mode (no mouse emulation)
            # - Disable the watchdog timer (which would re-enable lizard mode)
            settings_cmd = [
                self.ID_SET_SETTINGS_VALUES,
                3,  # Number of settings to set
                self.SETTING_LEFT_TRACKPAD_MODE,
                self.TRACKPAD_NONE,
                self.SETTING_RIGHT_TRACKPAD_MODE,
                self.TRACKPAD_NONE,
                self.SETTING_STEAM_WATCHDOG_ENABLE,
                0,
            ]
            if not self._send_feature_report(settings_cmd):
                self._log_warning("hidraw: failed to set controller settings")

            self._initialized = True
            self._log_info("hidraw: controller initialized for button monitoring")
            return True

        except Exception as e:
            self._log_error(f"hidraw: failed to initialize device: {e}")
            self._close_device()
            return False

    def _close_device(self):
        """Safely close the device file descriptor for reconnection."""
        if self._device_fd is not None:
            try:
                os.close(self._device_fd)
            except OSError:
                pass
            self._device_fd = None
        self._initialized = False
        self._device_path = None

    # -------------------------------------------------------------------------
    # Public API: start / stop / configure
    # -------------------------------------------------------------------------

    def start(self):
        """
        Start the background monitoring thread.

        Attempts to find and initialize the hidraw device, then starts a
        daemon thread that reads HID packets and detects button holds.

        Returns:
            True if the monitor started successfully, False if the device
            was not found (the plugin will still work via UI — this is a
            graceful degradation).
        """
        if self._running:
            self._log_warning("hidraw: monitor already running")
            return True

        if not self._initialize_device():
            self._log_warning("hidraw: device not found — button trigger disabled")
            return False

        self._running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()
        self._log_info("hidraw: monitor started")
        return True

    def stop(self):
        """
        Stop the monitoring thread and close the device.

        Safe to call even if the monitor isn't running (no-op).
        """
        self._running = False

        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

        self._close_device()
        self._log_info("hidraw: monitor stopped")

    def configure(self, target_button=None, hold_threshold_ms=None):
        """
        Update monitor configuration at runtime (thread-safe).

        Changes take effect on the next packet processing cycle (within ~4ms).
        Does not require stopping/restarting the monitor.

        Args:
            target_button: New target button name (e.g., "L4", "R4", "L5", "R5").
                           Pass None to keep the current value.
            hold_threshold_ms: New hold threshold in milliseconds (100-5000).
                               Pass None to keep the current value.
        """
        with self._lock:
            if target_button is not None:
                self._target_button = target_button
                # Reset hold tracking when button changes so we don't carry
                # over a partial hold from the previous button
                self._button_press_start = None
                self._triggered = False
                self._log_info(f"hidraw: target button changed to {target_button}")

            if hold_threshold_ms is not None:
                self._hold_threshold_s = hold_threshold_ms / 1000.0
                self._log_info(f"hidraw: hold threshold changed to {hold_threshold_ms}ms")

    # -------------------------------------------------------------------------
    # Status / diagnostics
    # -------------------------------------------------------------------------

    def get_button_state(self):
        """
        Get the set of currently pressed buttons.

        Returns:
            List of button name strings (e.g., ["L4", "A"]).
        """
        with self._lock:
            return list(self._current_buttons)

    def get_status(self):
        """
        Get monitor status for UI display and diagnostics.

        Returns:
            Dict with keys: running, initialized, device_path, error_count,
            target_button, hold_threshold_ms.
        """
        with self._lock:
            return {
                "running": self._running,
                "initialized": self._initialized,
                "device_path": self._device_path,
                "error_count": self._error_count,
                "target_button": self._target_button,
                "hold_threshold_ms": int(self._hold_threshold_s * 1000),
            }

    # -------------------------------------------------------------------------
    # Monitor thread: main loop
    # -------------------------------------------------------------------------

    def _monitor_loop(self):
        """
        Background thread main loop — reads HID packets and detects holds.

        Runs continuously until self._running is set to False. Uses select()
        with a 0.1s timeout so the thread checks the running flag regularly
        and can shut down cleanly.

        Auto-reconnects if the device is disconnected or too many read errors
        occur (e.g., controller unplugged and re-plugged).
        """
        self._log_info("hidraw: monitor loop started")
        reconnect_delay = 2.0
        max_errors = 10

        while self._running:
            try:
                # Reconnect if device is not initialized
                if not self._initialized or self._device_fd is None:
                    self._log_info("hidraw: attempting reconnect...")
                    if not self._initialize_device():
                        time.sleep(reconnect_delay)
                        continue

                # Wait for data with a timeout (allows checking self._running)
                r, _, _ = select.select([self._device_fd], [], [], 0.1)
                if not r:
                    # No data available — but still check hold timing.
                    # This ensures we fire the trigger even if no new packets
                    # arrive while the button is being held down.
                    self._check_hold_trigger()
                    continue

                # Read a HID packet
                data = os.read(self._device_fd, self.PACKET_SIZE)
                if len(data) >= 16:
                    self._process_packet(data)
                    self._error_count = 0

            except OSError as e:
                self._error_count += 1
                self._log_warning(f"hidraw: read error ({self._error_count}): {e}")

                if self._error_count >= max_errors:
                    self._log_error("hidraw: too many errors, closing device for reconnection")
                    self._close_device()
                    # Reset hold state since we lost the device
                    self._button_press_start = None
                    self._triggered = False
                    time.sleep(reconnect_delay)

            except Exception as e:
                self._error_count += 1
                self._log_error(f"hidraw: unexpected error in monitor loop: {e}")
                time.sleep(0.1)

        self._log_info("hidraw: monitor loop ended")

    # -------------------------------------------------------------------------
    # Packet processing and hold detection
    # -------------------------------------------------------------------------

    def _process_packet(self, data):
        """
        Parse a HID packet and update button state + hold detection.

        Each packet contains two 32-bit bitmasks representing which buttons
        are currently pressed. We compare against the previous state to
        detect changes, then check if the target button is being held.

        Args:
            data: Raw bytes of the HID packet (at least 16 bytes).
        """
        # Parse button bitmasks from the packet
        buttons_l = struct.unpack("<I", data[8:12])[0]
        buttons_h = struct.unpack("<I", data[12:16])[0]

        # Skip processing if nothing changed (common case — most packets
        # are identical when the user isn't pressing anything)
        if buttons_l == self._last_buttons_l and buttons_h == self._last_buttons_h:
            # Even if state didn't change, check hold timing
            self._check_hold_trigger()
            return

        # Decode which buttons are currently pressed
        new_buttons = set()
        for name, mask in BUTTONS_L.items():
            if buttons_l & mask:
                new_buttons.add(name)
        for name, mask in BUTTONS_H.items():
            if buttons_h & mask:
                new_buttons.add(name)

        # Update state (lock protects against get_button_state() reads)
        with self._lock:
            old_buttons = self._current_buttons
            self._current_buttons = new_buttons
            target = self._target_button

        # Save raw values for next comparison
        self._last_buttons_l = buttons_l
        self._last_buttons_h = buttons_h

        # --- Hold detection logic ---
        target_is_pressed = target in new_buttons
        target_was_pressed = target in old_buttons

        if target_is_pressed and not target_was_pressed:
            # Target button just pressed — start hold timer
            self._button_press_start = time.monotonic()
            self._triggered = False

        elif not target_is_pressed and target_was_pressed:
            # Target button just released — reset hold state
            self._button_press_start = None
            self._triggered = False

        # Check if hold threshold is met
        self._check_hold_trigger()

    def _check_hold_trigger(self):
        """
        Check if the target button has been held long enough to fire.

        Called both from _process_packet() (when state changes) and from
        the main loop (on select timeout, when no new packets arrive).
        This ensures the trigger fires promptly even if the user holds
        the button perfectly still (no new HID events).
        """
        if self._button_press_start is None:
            return  # Target button is not pressed

        if self._triggered:
            return  # Already fired for this press

        now = time.monotonic()

        # Check cooldown — prevents rapid re-triggering
        if now < self._cooldown_until:
            return

        # Read threshold under lock (may be changed by configure())
        with self._lock:
            threshold = self._hold_threshold_s

        held_duration = now - self._button_press_start

        if held_duration >= threshold:
            # Hold threshold reached — fire the trigger!
            self._triggered = True
            self._cooldown_until = now + self._cooldown_duration

            self._log_info(f"hidraw: button hold trigger fired " f"(held {held_duration:.2f}s >= {threshold:.2f}s)")

            if self._on_trigger:
                try:
                    self._on_trigger()
                except Exception as e:
                    self._log_error(f"hidraw: trigger callback error: {e}")
