# =============================================================================
# Decky Cloud Reader — Touchscreen Monitor
# =============================================================================
#
# Self-contained module for reading touch input from the Steam Deck's
# capacitive touchscreen via the Linux evdev interface (/dev/input/eventN).
#
# Uses only stdlib modules (os, struct, fcntl, select, time, threading) —
# no pip dependencies needed. This is important because Decky's embedded
# Python can't install pip packages.
#
# How it works:
#
#   The Steam Deck's touchscreen is a multitouch capacitive panel that reports
#   events via the Linux input subsystem (evdev). Each event is a 24-byte
#   struct on x86_64:
#
#     struct input_event {
#         struct timeval time;  // 16 bytes (two 64-bit longs on x86_64)
#         __u16 type;           // Event type (EV_ABS, EV_SYN, etc.)
#         __u16 code;           // Event code (ABS_MT_POSITION_X, etc.)
#         __s32 value;          // Event value (coordinate, tracking ID, etc.)
#     };
#
#   The monitor reads these events in a background daemon thread, tracks
#   the first finger's position, and fires a callback on quick taps (touch
#   down → touch up within a configurable timeout).
#
# Coordinate system:
#
#   The touchscreen hardware reports in its native (portrait) orientation:
#     - Physical X: 0 to ~1200 (short axis, physical width)
#     - Physical Y: 0 to ~1920 (long axis, physical height)
#
#   Steam Deck's screen is used in landscape mode (rotated 90° CW), so:
#     - Logical X (horizontal, 0-1280) = Physical Y (scaled)
#     - Logical Y (vertical, 0-800)   = Physical width - Physical X (scaled)
#
#   We read the actual axis ranges from the device via EVIOCGABS ioctl so
#   we don't hardcode hardware-specific values.
#
# Device discovery:
#
#   The touchscreen appears as /dev/input/eventN. We scan /sys/class/input/
#   for devices whose name matches the Steam Deck's touchscreen controller
#   ("FTS3528:00 2808:1015").
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
# Linux input event constants
# =============================================================================
# These are standard Linux kernel constants from <linux/input.h> and
# <linux/input-event-codes.h>. We define them here rather than importing
# from a system module because Decky's embedded Python may not have the
# evdev package installed.

# Event types
EV_SYN = 0x00  # Synchronization event (marks end of a group of events)
EV_ABS = 0x03  # Absolute axis event (touch coordinates, tracking IDs)

# Absolute axis codes for multitouch (MT) protocol B
ABS_MT_SLOT = 0x2F  # MT slot index (which finger)
ABS_MT_TRACKING_ID = 0x39  # Unique tracking ID per finger (>=0 = touch, -1 = lift)
ABS_MT_POSITION_X = 0x35  # X coordinate of touch point
ABS_MT_POSITION_Y = 0x36  # Y coordinate of touch point

# Size of struct input_event on x86_64 Linux.
# Layout: struct timeval (16 bytes) + __u16 type + __u16 code + __s32 value = 24 bytes.
# On x86_64, timeval is two 64-bit longs (tv_sec + tv_usec).
INPUT_EVENT_SIZE = 24
INPUT_EVENT_FORMAT = "llHHi"  # two longs + unsigned short + unsigned short + signed int

# Steam Deck touchscreen device name (as reported in /sys/class/input/*/device/name)
TOUCHSCREEN_DEVICE_NAME = "FTS3528:00 2808:1015"


# EVIOCGABS ioctl — reads struct input_absinfo for an axis.
# Formula: _IOR('E', 0x40 + axis, struct input_absinfo)
# struct input_absinfo has 6 x int32 fields = 24 bytes.
# _IOR means read from device: direction bits = 0x80000000
# Size field: 24 << 16 = 0x00180000
# Type field: ord('E') << 8 = 0x4500
def _eviocgabs(axis):
    """Build the EVIOCGABS ioctl number for a given axis code."""
    return 0x80000000 | (24 << 16) | (ord("E") << 8) | (0x40 + axis)


# struct input_absinfo format: 6 x signed int32
ABSINFO_FORMAT = "iiiiii"  # value, min, max, fuzz, flat, resolution

# Steam Deck screen dimensions in landscape mode (logical pixels)
LOGICAL_WIDTH = 1280  # Horizontal (physical Y axis, the long one)
LOGICAL_HEIGHT = 800  # Vertical (physical X axis, the short one)


# =============================================================================
# TouchscreenMonitor class
# =============================================================================


class TouchscreenMonitor:
    """
    Monitors the Steam Deck touchscreen for tap events via /dev/input/eventN.

    Usage:
        monitor = TouchscreenMonitor(
            on_touch=lambda x, y: print(f"Tap at ({x}, {y})"),
            logger=decky.logger,
            log_prefix="[DCR]",
        )
        monitor.start()       # Starts background thread
        status = monitor.get_status()  # Check device status
        last = monitor.get_last_touch()  # Get last tap coordinates
        monitor.stop()        # Stops thread, closes device

    The on_touch callback is called from the monitor thread. If you need
    to run async code, use asyncio.run_coroutine_threadsafe() in the callback.
    """

    def __init__(self, on_touch=None, on_touch_down=None, on_touch_up=None, logger=None, log_prefix="[DCR]"):
        """
        Initialize the touchscreen monitor.

        Args:
            on_touch: Callable(x, y) invoked from the monitor thread when a
                      short tap is detected (duration < 0.5s). x/y are in
                      logical screen coordinates (0-1280 horizontal, 0-800 vertical).
                      Legacy callback — kept for backward compatibility.
            on_touch_down: Callable(x, y) invoked when a finger first contacts
                          the screen. Fired at SYN_REPORT boundary to ensure
                          coordinates are fresh. x/y are logical coordinates.
            on_touch_up: Callable(end_x, end_y, start_x, start_y, duration)
                        invoked every time a finger lifts — regardless of
                        duration. Provides both start and end coordinates plus
                        the touch duration in seconds.
            logger: Logger object with .info(), .debug(), .warning(), .error()
                    methods. If None, logging is silently skipped.
            log_prefix: String prefix for log messages (e.g., "[DCR]").
        """
        # Callbacks
        self._on_touch = on_touch  # Legacy: short taps only
        self._on_touch_down = on_touch_down  # Phase 12: finger contact
        self._on_touch_up = on_touch_up  # Phase 12: finger lift (always)

        # Logging
        self._logger = logger
        self._log_prefix = log_prefix

        # Device state
        self._device_fd = None
        self._device_path = None
        self._initialized = False

        # Physical axis ranges (read from device via ioctl)
        self._physical_max_x = 0  # Max value for physical X axis (short axis)
        self._physical_max_y = 0  # Max value for physical Y axis (long axis)

        # Thread state
        self._running = False
        self._thread = None
        self._lock = threading.Lock()

        # Touch tracking state (only accessed from monitor thread, no lock needed)
        # We track slot 0 only (first finger) for Phase 9 simplicity.
        self._current_slot = 0  # Current MT slot being updated
        self._touch_active = False  # Whether slot 0 has an active touch
        self._touch_x = 0  # Current physical X of slot 0
        self._touch_y = 0  # Current physical Y of slot 0
        self._touch_start_time = 0.0  # monotonic timestamp when touch started

        # Phase 12: Deferred touch-down state. ABS_MT_TRACKING_ID may arrive
        # before position events in the same SYN_REPORT frame, so we defer
        # firing on_touch_down until SYN_REPORT ensures coordinates are fresh.
        self._touch_down_pending = False  # True = fire on_touch_down at next SYN_REPORT
        self._touch_start_x = 0  # Physical X at finger contact
        self._touch_start_y = 0  # Physical Y at finger contact

        # Last detected tap in logical coordinates (protected by _lock)
        self._last_touch_logical = None  # dict {x, y} or None

        # Error tracking for auto-reconnect
        self._error_count = 0

        # Timing parameters
        self._tap_timeout = 0.5  # Max seconds for a touch to count as a tap
        self._cooldown_duration = 0.3  # Seconds to ignore taps after one fires
        self._cooldown_until = 0.0  # monotonic timestamp — ignore taps until this

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
        Find the Steam Deck touchscreen input device.

        Scans /sys/class/input/ for event devices whose name matches
        TOUCHSCREEN_DEVICE_NAME ("FTS3528:00 2808:1015").

        Returns:
            Device path string (e.g., "/dev/input/event5") or None if not found.
        """
        try:
            # List all input devices (event0, event1, ...)
            input_dir = "/sys/class/input"
            if not os.path.isdir(input_dir):
                self._log_warning("touch: /sys/class/input not found")
                return None

            for entry in os.listdir(input_dir):
                # Only look at eventN entries (not mice, js0, etc.)
                if not entry.startswith("event"):
                    continue

                # Read the device name from sysfs
                name_path = os.path.join(input_dir, entry, "device", "name")
                try:
                    with open(name_path, "r") as f:
                        device_name = f.read().strip()
                except (OSError, IOError):
                    continue

                if device_name == TOUCHSCREEN_DEVICE_NAME:
                    dev_path = f"/dev/input/{entry}"
                    if os.path.exists(dev_path):
                        self._log_info(f"touch: found touchscreen at {dev_path} ({device_name})")
                        return dev_path
                    else:
                        self._log_warning(f"touch: found device in sysfs but {dev_path} doesn't exist")

        except Exception as e:
            self._log_error(f"touch: device discovery error: {e}")

        self._log_warning("touch: Steam Deck touchscreen not found")
        return None

    # -------------------------------------------------------------------------
    # Device initialization
    # -------------------------------------------------------------------------

    def _initialize_device(self):
        """
        Open the evdev device and read axis dimensions.

        Unlike hidraw (which needs write access for feature reports), the
        touchscreen only needs read-only access. We use O_NONBLOCK so reads
        don't block — we use select() for timeout-based polling instead.

        After opening, we read the physical axis ranges via EVIOCGABS ioctl
        so we can correctly map physical coordinates to logical screen
        coordinates.

        Returns:
            True if the device was opened and dimensions read, False otherwise.
        """
        if self._device_path is None:
            self._device_path = self._find_device()
            if self._device_path is None:
                return False

        try:
            # Open read-only + non-blocking
            self._device_fd = os.open(self._device_path, os.O_RDONLY | os.O_NONBLOCK)
            self._log_info(f"touch: opened {self._device_path}")

            # Read physical axis ranges via EVIOCGABS ioctl.
            # struct input_absinfo = { value, min, max, fuzz, flat, resolution }
            # We need the 'max' field (index 2) for coordinate scaling.

            # Read X axis info (ABS_MT_POSITION_X)
            buf_x = fcntl.ioctl(
                self._device_fd, _eviocgabs(ABS_MT_POSITION_X), b"\x00" * struct.calcsize(ABSINFO_FORMAT)
            )
            absinfo_x = struct.unpack(ABSINFO_FORMAT, buf_x)
            self._physical_max_x = absinfo_x[2]  # max field

            # Read Y axis info (ABS_MT_POSITION_Y)
            buf_y = fcntl.ioctl(
                self._device_fd, _eviocgabs(ABS_MT_POSITION_Y), b"\x00" * struct.calcsize(ABSINFO_FORMAT)
            )
            absinfo_y = struct.unpack(ABSINFO_FORMAT, buf_y)
            self._physical_max_y = absinfo_y[2]  # max field

            self._log_info(
                f"touch: axis ranges — physical X: 0-{self._physical_max_x}, " f"physical Y: 0-{self._physical_max_y}"
            )

            # Sanity check — these should be non-zero
            if self._physical_max_x == 0 or self._physical_max_y == 0:
                self._log_error("touch: axis range is 0 — device may not be a touchscreen")
                self._close_device()
                return False

            self._initialized = True
            self._log_info("touch: touchscreen initialized for tap detection")
            return True

        except Exception as e:
            self._log_error(f"touch: failed to initialize device: {e}")
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
    # Coordinate transformation
    # -------------------------------------------------------------------------

    def _physical_to_logical(self, phys_x, phys_y):
        """
        Convert physical touchscreen coordinates to logical screen coordinates.

        The Steam Deck's panel is physically in portrait orientation but used
        in landscape. The physical-to-logical mapping is a 90° clockwise
        rotation:

          Logical X (0 → 1280) = Physical Y scaled to logical width
          Logical Y (0 → 800)  = (Physical max X - Physical X) scaled to logical height

        This means:
          - Physical top-left (0, 0) → logical bottom-left (0, 800)
          - Physical top-right (max_x, 0) → logical top-left (0, 0)
          - Physical bottom-right (max_x, max_y) → logical top-right (1280, 0)

        Args:
            phys_x: Physical X coordinate (0 to physical_max_x)
            phys_y: Physical Y coordinate (0 to physical_max_y)

        Returns:
            Tuple (logical_x, logical_y) in screen coordinates.
        """
        if self._physical_max_x == 0 or self._physical_max_y == 0:
            return (0, 0)

        # Scale physical Y → logical X (horizontal)
        logical_x = int(phys_y * LOGICAL_WIDTH / self._physical_max_y)

        # Scale inverted physical X → logical Y (vertical)
        logical_y = int((self._physical_max_x - phys_x) * LOGICAL_HEIGHT / self._physical_max_x)

        # Clamp to valid range
        logical_x = max(0, min(LOGICAL_WIDTH, logical_x))
        logical_y = max(0, min(LOGICAL_HEIGHT, logical_y))

        return (logical_x, logical_y)

    # -------------------------------------------------------------------------
    # Public API: start / stop / get_status / get_last_touch
    # -------------------------------------------------------------------------

    def start(self):
        """
        Start the background monitoring thread.

        Attempts to find and initialize the touchscreen device, then starts a
        daemon thread that reads input events and detects taps.

        Returns:
            True if the monitor started successfully, False if the device
            was not found (the plugin still works — this is graceful degradation).
        """
        if self._running:
            self._log_warning("touch: monitor already running")
            return True

        if not self._initialize_device():
            self._log_warning("touch: device not found — touchscreen input disabled")
            return False

        self._running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()
        self._log_info("touch: monitor started")
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
        self._log_info("touch: monitor stopped")

    def get_status(self):
        """
        Get monitor status for UI display and diagnostics.

        Returns:
            Dict with keys: running, initialized, device_path, error_count,
            physical_width, physical_height, last_touch.
        """
        with self._lock:
            return {
                "running": self._running,
                "initialized": self._initialized,
                "device_path": self._device_path,
                "error_count": self._error_count,
                "physical_max_x": self._physical_max_x,
                "physical_max_y": self._physical_max_y,
                "last_touch": self._last_touch_logical,
            }

    def get_last_touch(self):
        """
        Get the coordinates of the last detected tap.

        Returns:
            Dict {x, y} in logical screen coordinates, or None if no tap
            has been detected yet.
        """
        with self._lock:
            return self._last_touch_logical

    # -------------------------------------------------------------------------
    # Monitor thread: main loop
    # -------------------------------------------------------------------------

    def _monitor_loop(self):
        """
        Background thread main loop — reads input events and detects taps.

        Runs continuously until self._running is set to False. Uses select()
        with a 0.1s timeout so the thread checks the running flag regularly
        and can shut down cleanly.

        Auto-reconnects if the device is disconnected or too many read errors
        occur.
        """
        self._log_info("touch: monitor loop started")
        reconnect_delay = 2.0
        max_errors = 10

        while self._running:
            try:
                # Reconnect if device is not initialized
                if not self._initialized or self._device_fd is None:
                    self._log_info("touch: attempting reconnect...")
                    if not self._initialize_device():
                        time.sleep(reconnect_delay)
                        continue

                # Wait for data with a timeout (allows checking self._running)
                r, _, _ = select.select([self._device_fd], [], [], 0.1)
                if not r:
                    continue  # No data — loop back and check _running

                # Read available data — may contain multiple events
                try:
                    data = os.read(self._device_fd, INPUT_EVENT_SIZE * 64)
                except BlockingIOError:
                    continue  # O_NONBLOCK: no data available right now

                if not data:
                    continue

                # Process events in 24-byte chunks
                offset = 0
                while offset + INPUT_EVENT_SIZE <= len(data):
                    chunk = data[offset : offset + INPUT_EVENT_SIZE]
                    offset += INPUT_EVENT_SIZE

                    # Unpack: two longs (timeval) + unsigned short (type) +
                    #          unsigned short (code) + signed int (value)
                    _tv_sec, _tv_usec, ev_type, ev_code, ev_value = struct.unpack(INPUT_EVENT_FORMAT, chunk)

                    self._process_event(ev_type, ev_code, ev_value)

                self._error_count = 0

            except OSError as e:
                self._error_count += 1
                self._log_warning(f"touch: read error ({self._error_count}): {e}")

                if self._error_count >= max_errors:
                    self._log_error("touch: too many errors, closing device for reconnection")
                    self._close_device()
                    # Reset touch state since we lost the device
                    self._touch_active = False
                    time.sleep(reconnect_delay)

            except Exception as e:
                self._error_count += 1
                self._log_error(f"touch: unexpected error in monitor loop: {e}")
                time.sleep(0.1)

        self._log_info("touch: monitor loop ended")

    # -------------------------------------------------------------------------
    # Event processing
    # -------------------------------------------------------------------------

    def _process_event(self, ev_type, ev_code, ev_value):
        """
        Process a single input event.

        Multitouch protocol B (Linux kernel) works like this:
          1. ABS_MT_SLOT selects which finger slot is being updated
          2. ABS_MT_TRACKING_ID >= 0 means a new touch in this slot
          3. ABS_MT_TRACKING_ID == -1 means the finger lifted from this slot
          4. ABS_MT_POSITION_X/Y update the coordinates of the current slot
          5. EV_SYN marks the end of a batch of updates

        We only track slot 0 (first finger) for Phase 9 simplicity.

        Phase 12 enhancement: on_touch_down is deferred until the SYN_REPORT
        event so that position coordinates are guaranteed to be fresh.

        Args:
            ev_type: Event type (EV_ABS, EV_SYN, etc.)
            ev_code: Event code (ABS_MT_POSITION_X, etc.)
            ev_value: Event value (coordinate value, tracking ID, etc.)
        """
        # Phase 12: Handle SYN_REPORT — end of an event frame. This is where
        # we fire the deferred on_touch_down callback, after all position
        # events in the same frame have been processed.
        if ev_type == EV_SYN and ev_code == 0:
            self._handle_syn_report()
            return

        if ev_type != EV_ABS:
            return  # We only care about absolute axis events

        if ev_code == ABS_MT_SLOT:
            # Switch to tracking a different finger slot
            self._current_slot = ev_value
            return

        # Only process events for slot 0 (first finger)
        if self._current_slot != 0:
            return

        if ev_code == ABS_MT_TRACKING_ID:
            if ev_value >= 0:
                # Touch down — new finger contact on slot 0.
                # Don't fire on_touch_down yet — wait for SYN_REPORT so
                # position coordinates in the same frame are captured first.
                self._touch_active = True
                self._touch_start_time = time.monotonic()
                self._touch_down_pending = True
                self._log_debug(f"touch: finger down (tracking_id={ev_value})")
            else:
                # Touch up — finger lifted from slot 0 (value == -1)
                if self._touch_active:
                    self._handle_touch_up()
                self._touch_active = False
                self._touch_down_pending = False

        elif ev_code == ABS_MT_POSITION_X:
            # Update current X coordinate (only meaningful if touch is active)
            self._touch_x = ev_value

        elif ev_code == ABS_MT_POSITION_Y:
            # Update current Y coordinate
            self._touch_y = ev_value

    def _handle_syn_report(self):
        """
        Called at the SYN_REPORT boundary (end of an event frame).

        If a touch-down was pending (ABS_MT_TRACKING_ID >= 0 arrived in this
        frame), we now have fresh position coordinates, so it's safe to fire
        the on_touch_down callback and record the start position.
        """
        if self._touch_down_pending and self._touch_active:
            self._touch_down_pending = False
            # Record starting position in physical coordinates
            self._touch_start_x = self._touch_x
            self._touch_start_y = self._touch_y

            # Fire on_touch_down callback with logical coordinates
            if self._on_touch_down:
                logical_x, logical_y = self._physical_to_logical(self._touch_start_x, self._touch_start_y)
                self._log_debug(f"touch: on_touch_down ({logical_x}, {logical_y})")
                try:
                    self._on_touch_down(logical_x, logical_y)
                except Exception as e:
                    self._log_error(f"touch: on_touch_down callback error: {e}")

    def _handle_touch_up(self):
        """
        Called when a finger lifts from slot 0.

        Phase 12 enhancement: ALWAYS fires on_touch_up with start/end
        coordinates and duration — no duration filter. Then fires the legacy
        on_touch callback only for short taps (duration < tap_timeout, with
        cooldown), preserving backward compatibility.
        """
        now = time.monotonic()
        duration = now - self._touch_start_time

        # Compute start position in logical coordinates (recorded at touch-down)
        start_x, start_y = self._physical_to_logical(self._touch_start_x, self._touch_start_y)
        # Compute end position in logical coordinates (current finger position)
        end_x, end_y = self._physical_to_logical(self._touch_x, self._touch_y)

        self._log_info(f"touch: finger up — start=({start_x},{start_y}) end=({end_x},{end_y}) " f"held={duration:.2f}s")

        # Phase 12: ALWAYS fire on_touch_up — no duration or cooldown filter.
        # This provides raw touch-up data for swipe detection, two-tap mode, etc.
        if self._on_touch_up:
            try:
                self._on_touch_up(end_x, end_y, start_x, start_y, duration)
            except Exception as e:
                self._log_error(f"touch: on_touch_up callback error: {e}")

        # Legacy on_touch callback: only fires for short taps with cooldown.
        # This preserves the Phase 9 behavior for backward compatibility and
        # is used by two-tap and hybrid capture modes.
        if now < self._cooldown_until:
            self._log_debug("touch: tap ignored (cooldown)")
            return

        if duration > self._tap_timeout:
            self._log_debug(f"touch: not a tap (held {duration:.2f}s > {self._tap_timeout}s)")
            return

        # Store the last tap coordinates (thread-safe)
        with self._lock:
            self._last_touch_logical = {"x": end_x, "y": end_y}

        # Set cooldown to prevent rapid re-triggering
        self._cooldown_until = now + self._cooldown_duration

        self._log_info(
            f"touch: tap detected at ({end_x}, {end_y}) "
            f"[physical ({self._touch_x}, {self._touch_y}), held {duration:.2f}s]"
        )

        # Fire legacy tap callback
        if self._on_touch:
            try:
                self._on_touch(end_x, end_y)
            except Exception as e:
                self._log_error(f"touch: on_touch callback error: {e}")
