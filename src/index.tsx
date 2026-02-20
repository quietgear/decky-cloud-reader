// =============================================================================
// Decky Cloud Reader — Frontend Entry Point
// =============================================================================
//
// This file defines the plugin's UI that appears in the Decky Loader sidebar.
// It uses React components from @decky/ui (Steam's design system) and the
// @decky/api bridge to call Python backend methods via RPC.
//
// The UI has two modes:
//   1. Normal mode — Shows credential status, settings toggles, and buttons
//      to load/clear GCP credentials.
//   2. File browser mode — A custom file browser that lets the user navigate
//      the Steam Deck's filesystem to find and select a GCP service account
//      JSON file. This avoids typing long paths on the virtual keyboard.
// =============================================================================

import {
  ButtonItem,       // A clickable button styled for the Steam UI
  PanelSection,     // A collapsible section in the plugin sidebar panel
  PanelSectionRow,  // A row within a PanelSection
  ToggleField,      // A toggle switch with label and description
  Field,            // Generic field container with label support
  DropdownItem,     // A dropdown selector for picking from a list of options
  SliderField,      // A slider for numeric values with min/max/step
  TextField,        // Text input field — triggers Steam on-screen keyboard on focus
  showModal,        // Opens a full-screen modal dialog (needed for keyboard focus in QAM)
  ModalRoot,        // Container for modal content with onCancel/onEscKeypress handlers
  DialogButton,     // Button styled for modal dialogs (Save/Cancel)
  Focusable,        // Makes elements gamepad-focusable, supports flow-children layout
  staticClasses,    // CSS class names for standard Steam UI styling
  findModuleChild,  // Searches Steam's internal modules for hidden hooks/utilities
  useQuickAccessVisible // Returns true/false when QAM ("..." menu) opens/closes
} from "@decky/ui";

import {
  callable,         // Creates a typed function that calls a Python backend method
  definePlugin,     // Registers this module as a Decky plugin
  routerHook        // Global component registration for overlays outside the QAM panel
} from "@decky/api";

import { useState, useEffect, useRef } from "react";

// FaBook icon — fits the "reader" theme of this plugin.
// FaFolder/FaFile icons — used in the file browser for visual clarity.
import { FaBook, FaFolder, FaFileAlt, FaArrowLeft } from "react-icons/fa";

// Build-time version injected by @rollup/plugin-replace from package.json
declare const __PLUGIN_VERSION__: string;
const PLUGIN_VERSION = __PLUGIN_VERSION__;


// =============================================================================
// UIComposition — Steam's internal composition layer system (Phase 13)
// =============================================================================
// Gamescope uses composition layers to control what renders on top of the game.
// We need to request a composition layer so the overlay is visible above the
// game but below full opaque overlays (like the on-screen keyboard).
//
// The useUIComposition hook is not publicly exported by @decky/ui, so we use
// findModuleChild to locate it by matching the function signature in Steam's
// internal module system.

enum UIComposition {
  Hidden = 0,           // Not visible
  Notification = 1,     // Above game, below full overlays (what we want)
  Overlay = 2,          // Standard overlay layer
  Opaque = 3,           // Fully opaque (blocks everything below)
  OverlayKeyboard = 4,  // On-screen keyboard layer
}

// Search Steam's module registry for the useUIComposition hook.
// It's identified by three method name strings it uses internally to manage
// composition state requests with Gamescope's compositor.
const useUIComposition: (composition: UIComposition) => void = findModuleChild(
  (m: any) => {
    if (typeof m !== "object") return undefined;
    for (let prop in m) {
      if (
        typeof m[prop] === "function" &&
        m[prop].toString().includes("AddMinimumCompositionStateRequest") &&
        m[prop].toString().includes("ChangeMinimumCompositionStateRequest") &&
        m[prop].toString().includes("RemoveMinimumCompositionStateRequest") &&
        !m[prop].toString().includes("m_mapCompositionStateRequests")
      ) {
        return m[prop];
      }
    }
  }
);

// =============================================================================
// VirtualKeyboardManager — detect on-screen keyboard open/close (Phase 14)
// =============================================================================
// Steam's internal module system exposes a VirtualKeyboardManager with an
// observable m_bIsVirtualKeyboardOpen property. We register a callback so
// touch gestures (two-tap, swipe) can be suppressed while the keyboard is up.
// Pattern from: https://github.com/CarJem/Decky-OSKPlus/blob/main/src/keyboard.tsx

const VIRTUAL_KEYBOARD_MANAGER = findModuleChild((m: any) => {
  if (typeof m !== "object") return undefined;
  for (let prop in m) {
    if (m[prop]?.m_WindowStore)
      return m[prop].ActiveWindowInstance?.VirtualKeyboardManager;
  }
});

// =============================================================================
// TypeScript interfaces — describe the shape of data from the Python backend
// =============================================================================

// A single entry in a directory listing (file or folder)
interface DirectoryEntry {
  name: string;     // File/folder name (e.g., "credentials.json")
  is_dir: boolean;  // true = directory, false = file
  size: number;     // File size in bytes (0 for directories)
}

// Response from the list_directory() backend RPC
interface DirectoryListing {
  path: string;                  // The absolute path that was listed
  entries: DirectoryEntry[];     // Array of entries in the directory
  error: string | null;          // Error message, or null if successful
}

// Response from the load_credentials_file() backend RPC
interface CredentialResult {
  valid: boolean;      // true if the file was a valid GCP service account JSON
  message: string;     // Human-readable success or error message
  project_id: string;  // GCP project ID (empty on error)
}

// Response from the get_button_monitor_status() backend RPC
interface ButtonMonitorStatus {
  running: boolean;          // true if the monitor thread is alive
  initialized: boolean;      // true if the hidraw device is open and initialized
  device_path: string | null;// e.g., "/dev/hidraw2" or null if not found
  error_count: number;       // consecutive read errors (0 = healthy)
  target_button: string;     // current target button (e.g., "L4") or "disabled"
  hold_threshold_ms: number; // current hold threshold in milliseconds
}

// Response from the get_touchscreen_status() backend RPC (Phase 9)
interface TouchscreenStatus {
  running: boolean;              // true if the monitor thread is alive
  initialized: boolean;          // true if the evdev device is open
  device_path: string | null;    // e.g., "/dev/input/event5" or null
  error_count: number;           // consecutive read errors (0 = healthy)
  physical_max_x: number;        // Physical X axis max (short axis)
  physical_max_y: number;        // Physical Y axis max (long axis)
  last_touch: { x: number; y: number } | null;  // Last tap in logical coords
}

// Info about a single Piper voice from get_available_voices()
interface VoiceInfo {
  label: string;       // Human-readable name (e.g., "US English - Amy (Female)")
  language: string;    // Language group (e.g., "English (US)")
  speakers: number;    // Number of speakers (1 = single, >1 = multi-speaker)
  downloaded: boolean; // Whether the .onnx file exists in the voices dir
  file_size: number;   // Size of the .onnx file in bytes (0 if not downloaded)
}

// Voice registry returned by get_available_voices(): voice_id → VoiceInfo
interface VoiceRegistry {
  [voice_id: string]: VoiceInfo;
}

// Response from download_voice() or delete_voice() backend RPCs
interface VoiceActionResult {
  success: boolean;
  message: string;
  file_size?: number;   // Only present in download response
}

// Response from the capture_overlay_screenshot() backend RPC (Phase 13)
interface OverlayScreenshotResult {
  success: boolean;       // true if screenshot was captured successfully
  image_base64: string;   // Base64-encoded PNG image data (empty on error)
  message: string;        // Human-readable success or error message
}

// Current plugin settings returned by get_settings() backend RPC
interface PluginSettings {
  // Provider selection (Phase 8)
  ocr_provider: string;         // "gcp" or "local"
  tts_provider: string;         // "gcp" or "local"
  // GCP TTS settings
  voice_id: string;             // GCP TTS voice (Phase 5)
  speech_rate: string;          // GCP TTS speed preset (Phase 5)
  // Local TTS settings (Phase 8)
  local_voice_id: string;       // Piper voice ID
  local_speech_rate: string;    // Piper speech rate preset
  // Common settings
  volume: number;               // TTS volume 0-100 (Phase 5)
  enabled: boolean;             // Master on/off
  debug: boolean;               // Verbose logging
  trigger_button: string;       // "disabled", "L4", "R4", "L5", "R5" (Phase 7)
  hold_time_ms: number;         // Hold threshold in ms (Phase 7)
  touchscreen_enabled: boolean; // Touchscreen tap input (Phase 9)
  // Capture mode (Phase 10/12)
  capture_mode: string;           // full_screen | swipe_selection | two_tap_selection | fixed_region | hybrid
  mute_interface_sounds: boolean; // Skip playing UI feedback sounds (Phase 10/11)
  // Fixed region coordinates (Phase 10/12)
  fixed_region_x1: number;
  fixed_region_y1: number;
  fixed_region_x2: number;
  fixed_region_y2: number;
  // Last selection coordinates (Phase 10/12)
  last_selection_x1: number;
  last_selection_y1: number;
  last_selection_x2: number;
  last_selection_y2: number;
  // Text filtering (Phase 10/13)
  ignored_words_always: string;
  ignored_words_always_enabled: boolean;
  ignored_words_beginning: string;
  ignored_words_beginning_enabled: boolean;
  ignored_words_count: number;
  // Computed fields
  is_configured: boolean;       // Whether current providers are ready
  is_gcp_configured: boolean;   // Whether GCP credentials are loaded
  is_local_available: boolean;  // Whether bundled Python 3.12 is present
  project_id: string;           // GCP project ID from credentials
}


// =============================================================================
// Backend RPC bindings
// =============================================================================
// Each `callable()` creates a typed function that sends an RPC to the Python
// backend (main.py). The string argument must match the Python method name.

// Get all settings (merged with defaults) + computed fields
const getSettings = callable<[], PluginSettings>("get_settings");

// Save a single setting by key
const saveSetting = callable<[string, any], boolean>("save_setting");

// List directory contents for the file browser
const listDirectory = callable<[string], DirectoryListing>("list_directory");

// Load and validate a GCP service account JSON file
const loadCredentialsFile = callable<[string], CredentialResult>("load_credentials_file");

// Clear stored GCP credentials
const clearCredentials = callable<[], boolean>("clear_credentials");

// Phase 7: Get button monitor status (running, device_path, error_count, etc.)
const getButtonMonitorStatus = callable<[], ButtonMonitorStatus>("get_button_monitor_status");

// Phase 8.6: Voice management — on-demand Piper voice downloads
const getAvailableVoices = callable<[], VoiceRegistry>("get_available_voices");
const downloadVoice = callable<[string], VoiceActionResult>("download_voice");
const deleteVoice = callable<[string], VoiceActionResult>("delete_voice");

// Phase 9: Touchscreen monitor status
const getTouchscreenStatus = callable<[], TouchscreenStatus>("get_touchscreen_status");

// Phase 11: Interface sound effects — fire-and-forget UI feedback sounds
const playInterfaceSound = callable<[string], {success: boolean; error?: string}>("play_interface_sound");

// Phase 12: Copy last_selection coordinates to fixed_region coordinates
const applyLastSelectionToFixedRegion = callable<[], {success: boolean; message: string}>(
  "apply_last_selection_to_fixed_region"
);

// Phase 13: Capture screenshot for the region preview overlay
const captureOverlayScreenshot = callable<[], OverlayScreenshotResult>("capture_overlay_screenshot");

// Phase 13.5: Notify backend when the on-screen keyboard opens/closes
// so touch gestures are suppressed while typing
const setKeyboardVisible = callable<[boolean], void>("set_keyboard_visible");

// Phase 14: Notify backend when a full-screen modal dialog opens/closes
// so touch gestures are suppressed while the modal is visible
const setModalVisible = callable<[boolean], void>("set_modal_visible");

// Phase 14: Notify backend when the QAM ("..." menu) opens/closes
// so touch gestures are suppressed while any part of the QAM is visible
const setQamVisible = callable<[boolean], void>("set_qam_visible");


// =============================================================================
// Helper: format file size in human-readable form
// =============================================================================
// Converts bytes to KB/MB for display in the file browser.
function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}


// =============================================================================
// OverlayState — shared state bridge for the region preview overlay (Phase 13)
// =============================================================================
// Uses an observer pattern to synchronize the Content component (which owns the
// toggle button) with the RegionPreviewOverlay global component (which renders
// outside the QAM panel). The Content component calls show()/hide(), and the
// overlay component listens via onChange() to update its rendering.
//
// This is the same pattern used by Decky-Translator's ImageState class.

// Fixed region coordinates for the overlay rectangle visualization
interface FixedRegion {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
}

class OverlayState {
  // Private state — only mutated via show() / hide()
  private _visible = false;
  private _imageBase64 = "";
  private _fixedRegion: FixedRegion = { x1: 0, y1: 0, x2: 1280, y2: 800 };

  // Observer callbacks — called whenever state changes
  private _listeners: Array<() => void> = [];

  // --- Public getters ---
  get visible(): boolean { return this._visible; }
  get imageBase64(): string { return this._imageBase64; }
  get fixedRegion(): FixedRegion { return this._fixedRegion; }

  // Show the overlay with a captured screenshot and the current fixed region
  show(imageBase64: string, region: FixedRegion): void {
    this._visible = true;
    this._imageBase64 = imageBase64;
    this._fixedRegion = region;
    this._notify();
  }

  // Hide the overlay and clear image data (frees memory)
  hide(): void {
    this._visible = false;
    this._imageBase64 = "";
    this._notify();
  }

  // Register a listener to be called on any state change
  onChange(cb: () => void): void {
    this._listeners.push(cb);
  }

  // Unregister a listener
  offChange(cb: () => void): void {
    const idx = this._listeners.indexOf(cb);
    if (idx !== -1) this._listeners.splice(idx, 1);
  }

  // Notify all listeners that state has changed
  private _notify(): void {
    for (const cb of this._listeners) cb();
  }
}


// =============================================================================
// RegionPreviewOverlay — global overlay showing fixed region on a screenshot
// =============================================================================
// Mounted/unmounted dynamically via routerHook.addGlobalComponent() and
// removeGlobalComponent(). This component only exists in the React tree while
// the overlay is visible — when hidden, it's completely removed. This avoids
// keeping a useUIComposition hook alive which would interfere with Gamescope's
// input routing on other Decky pages.
//
// Since the component is freshly mounted each time, it reads image data and
// region coordinates directly from the OverlayState props — no observer needed.

// Game screen dimensions (Steam Deck native resolution in landscape)
const GAME_WIDTH = 1280;
const GAME_HEIGHT = 800;

// Maximum size for the preview image. The QAM panel + icon sidebar starts at
// roughly x=560 on the 1280px screen. With a 20px left margin and ~20px gap
// before the icons, the preview fits at 500px wide.
const PREVIEW_MAX_WIDTH = 465;
// Maintain 16:10 aspect ratio (1280:800)
const PREVIEW_MAX_HEIGHT = PREVIEW_MAX_WIDTH * (GAME_HEIGHT / GAME_WIDTH);

function RegionPreviewOverlay({ state }: { state: OverlayState }) {
  // Request Notification composition layer from Gamescope so the overlay
  // renders above the game. This hook is only active while the component
  // is mounted (i.e., while the overlay is visible). When the component is
  // unmounted, the composition request is automatically cleaned up.
  useUIComposition(UIComposition.Notification);

  // Read current data directly from OverlayState (set before mounting)
  const imageBase64 = state.imageBase64;
  const fixedRegion = state.fixedRegion;

  // Calculate the scale factor and region position within the preview
  const scale = PREVIEW_MAX_WIDTH / GAME_WIDTH;
  const previewWidth = PREVIEW_MAX_WIDTH;
  const previewHeight = PREVIEW_MAX_HEIGHT;

  // Region rectangle in preview coordinates (scaled down proportionally)
  const regionLeft = fixedRegion.x1 * scale;
  const regionTop = fixedRegion.y1 * scale;
  const regionWidth = (fixedRegion.x2 - fixedRegion.x1) * scale;
  const regionHeight = (fixedRegion.y2 - fixedRegion.y1) * scale;

  return (
    <div
      style={{
        // Full viewport overlay container
        position: "fixed",
        top: 0,
        left: 0,
        width: "100vw",
        height: "100vh",
        zIndex: 7002,
        // Flex layout: vertically centered, pushed to the left edge
        display: "flex",
        justifyContent: "flex-start",
        alignItems: "center",
        // Small left margin so the preview doesn't touch the screen edge
        paddingLeft: "20px",
        backgroundColor: "transparent",
      }}
    >
      {/* Preview container: shrunk screenshot with region highlight */}
      {imageBase64 && (
        <div
          style={{
            position: "relative",
            width: `${previewWidth}px`,
            height: `${previewHeight}px`,
            borderRadius: "8px",
            overflow: "hidden",
            // Subtle border so the preview is visible against dark backgrounds
            border: "2px solid rgba(103, 183, 220, 0.6)",
          }}
        >
          {/* Shrunk game screenshot as background */}
          <img
            src={`data:image/png;base64,${imageBase64}`}
            style={{
              width: "100%",
              height: "100%",
              objectFit: "cover",
              display: "block",
            }}
            alt="Game screenshot"
          />

          {/* Dark overlay covering the entire image */}
          <div
            style={{
              position: "absolute",
              top: 0,
              left: 0,
              width: "100%",
              height: "100%",
              backgroundColor: "rgba(0, 0, 0, 0.55)",
            }}
          />

          {/* Region cutout — clear window showing the selected area.
              Uses a bright border and clears the dark overlay for this rectangle
              by rendering the same screenshot image clipped to the region bounds. */}
          <div
            style={{
              position: "absolute",
              left: `${regionLeft}px`,
              top: `${regionTop}px`,
              width: `${regionWidth}px`,
              height: `${regionHeight}px`,
              border: "2px solid #67b7dc",
              borderRadius: "2px",
              // Show the original image through the dark overlay
              overflow: "hidden",
            }}
          >
            {/* Re-render the screenshot, positioned so it aligns exactly
                with the background image but only shows through this cutout */}
            <img
              src={`data:image/png;base64,${imageBase64}`}
              style={{
                position: "absolute",
                // Offset the image so the visible portion matches the region
                left: `-${regionLeft}px`,
                top: `-${regionTop}px`,
                width: `${previewWidth}px`,
                height: `${previewHeight}px`,
                objectFit: "cover",
                display: "block",
              }}
              alt=""
            />
          </div>

          {/* Region size label at the bottom of the preview */}
          <div
            style={{
              position: "absolute",
              bottom: "4px",
              left: 0,
              right: 0,
              textAlign: "center",
              color: "#67b7dc",
              fontSize: "11px",
              textShadow: "0 1px 3px rgba(0,0,0,0.8)",
            }}
          >
            {fixedRegion.x2 - fixedRegion.x1} x {fixedRegion.y2 - fixedRegion.y1}
          </div>
        </div>
      )}
    </div>
  );
}


// =============================================================================
// Voice and speech rate options for the TTS dropdown selectors
// =============================================================================
// These match the VOICE_REGISTRY and SPEECH_RATE_MAP in gcp_worker.py.
// Each option has a `data` value (sent to the backend) and a `label` (shown in UI).

const VOICE_OPTIONS = [
  // English (US) - Neural2 voices
  { data: "en-US-Neural2-C", label: "English US Female (Neural)" },
  { data: "en-US-Neural2-D", label: "English US Male (Neural)" },
  { data: "en-US-Neural2-A", label: "English US Male 2 (Neural)" },
  { data: "en-US-Neural2-F", label: "English US Female 2 (Neural)" },
  // English (US) - Wavenet voices
  { data: "en-US-Wavenet-C", label: "English US Female (Wavenet)" },
  { data: "en-US-Wavenet-D", label: "English US Male (Wavenet)" },
  // English (UK) - Neural2 voices
  { data: "en-GB-Neural2-A", label: "English UK Female (Neural)" },
  { data: "en-GB-Neural2-B", label: "English UK Male (Neural)" },
  { data: "en-GB-Neural2-C", label: "English UK Female 2 (Neural)" },
  { data: "en-GB-Neural2-D", label: "English UK Male 2 (Neural)" },
  // Ukrainian - Wavenet and Standard voices
  { data: "uk-UA-Wavenet-A", label: "Ukrainian Female (Wavenet)" },
  { data: "uk-UA-Standard-A", label: "Ukrainian Female (Standard)" },
  // German - Neural2 voices
  { data: "de-DE-Neural2-A", label: "German Female (Neural)" },
  { data: "de-DE-Neural2-B", label: "German Male (Neural)" },
  { data: "de-DE-Neural2-C", label: "German Female 2 (Neural)" },
  { data: "de-DE-Neural2-D", label: "German Male 2 (Neural)" },
  // French - Neural2 voices
  { data: "fr-FR-Neural2-A", label: "French Female (Neural)" },
  { data: "fr-FR-Neural2-B", label: "French Male (Neural)" },
  { data: "fr-FR-Neural2-C", label: "French Female 2 (Neural)" },
  { data: "fr-FR-Neural2-D", label: "French Male 2 (Neural)" },
  // Spanish - Neural2 voices
  { data: "es-ES-Neural2-A", label: "Spanish Female (Neural)" },
  { data: "es-ES-Neural2-B", label: "Spanish Male (Neural)" },
  // Japanese - Neural2 voices
  { data: "ja-JP-Neural2-B", label: "Japanese Female (Neural)" },
  { data: "ja-JP-Neural2-C", label: "Japanese Male (Neural)" },
  { data: "ja-JP-Neural2-D", label: "Japanese Male 2 (Neural)" },
  // Portuguese (Brazil) - Neural2 voices
  { data: "pt-BR-Neural2-A", label: "Portuguese BR Female (Neural)" },
  { data: "pt-BR-Neural2-B", label: "Portuguese BR Male (Neural)" },
  { data: "pt-BR-Neural2-C", label: "Portuguese BR Female 2 (Neural)" },
  // Russian - Wavenet and Standard voices
  { data: "ru-RU-Wavenet-A", label: "Russian Female (Wavenet)" },
  { data: "ru-RU-Wavenet-B", label: "Russian Male (Wavenet)" },
  { data: "ru-RU-Standard-A", label: "Russian Female (Standard)" },
  { data: "ru-RU-Standard-B", label: "Russian Male (Standard)" },
];

const SPEECH_RATE_OPTIONS = [
  { data: "x-slow", label: "Very Slow (0.5x)" },
  { data: "slow",   label: "Slow (0.75x)" },
  { data: "medium", label: "Normal (1.0x)" },
  { data: "fast",   label: "Fast (1.25x)" },
  { data: "x-fast", label: "Very Fast (1.5x)" },
];


// =============================================================================
// Provider options for OCR and TTS engine selection (Phase 8)
// =============================================================================
// Users can choose between Google Cloud (online, requires credentials) and
// local inference (offline, uses bundled models).

const OCR_PROVIDER_OPTIONS = [
  { data: "local", label: "RapidOCR (offline)" },
  { data: "gcp",   label: "Google Cloud (online)" },
];

const TTS_PROVIDER_OPTIONS = [
  { data: "local", label: "Piper TTS (offline)" },
  { data: "gcp",   label: "Google Cloud (online)" },
];

// Speech rate options for Piper TTS. Same labels as GCP but maps to
// Piper's length_scale internally (inverse: lower = faster).
const LOCAL_SPEECH_RATE_OPTIONS = [
  { data: "x-slow", label: "Very Slow" },
  { data: "slow",   label: "Slow" },
  { data: "medium", label: "Normal" },
  { data: "fast",   label: "Fast" },
  { data: "x-fast", label: "Very Fast" },
];


// =============================================================================
// Button trigger options for the dropdown selectors (Phase 7)
// =============================================================================
// "disabled" turns off the hardware button trigger entirely.
// L4/R4/L5/R5 are the back grip buttons on the Steam Deck.

const TRIGGER_BUTTON_OPTIONS = [
  { data: "disabled", label: "Disabled" },
  { data: "L4",       label: "L4 (Back Left Upper)" },
  { data: "R4",       label: "R4 (Back Right Upper)" },
  { data: "L5",       label: "L5 (Back Left Lower)" },
  { data: "R5",       label: "R5 (Back Right Lower)" },
];

// Phase 12: Capture mode options for the dropdown selector
const CAPTURE_MODE_OPTIONS = [
  { data: "full_screen",        label: "Full Screen" },
  { data: "swipe_selection",    label: "Swipe Selection" },
  { data: "two_tap_selection",  label: "Two-Tap Selection" },
  { data: "fixed_region",       label: "Fixed Region" },
  { data: "hybrid",             label: "Hybrid (Fixed + Two-Tap)" },
];

const HOLD_TIME_OPTIONS = [
  { data: 0,    label: "Instant (0ms)" },
  { data: 300,  label: "300ms (Quick)" },
  { data: 500,  label: "500ms (Default)" },
  { data: 750,  label: "750ms" },
  { data: 1000, label: "1000ms (Long)" },
  { data: 1500, label: "1500ms (Very Long)" },
];


// =============================================================================
// FileBrowser component — lets the user navigate directories and pick a file
// =============================================================================
// This component renders a list of directories and .json files. The user can
// click directories to navigate into them, click ".." to go up, and click a
// .json file to load it as GCP credentials.

function FileBrowser({ onFileSelected, onCancel }: {
  onFileSelected: (path: string) => void;  // Called when user picks a .json file
  onCancel: () => void;                     // Called when user clicks Cancel
}) {
  // Current directory being displayed
  const [currentPath, setCurrentPath] = useState("/home/deck/");
  // Directory entries (files and folders) returned by the backend
  const [entries, setEntries] = useState<DirectoryEntry[]>([]);
  // Whether we're currently loading a directory listing
  const [loading, setLoading] = useState(true);
  // Error message from the backend (e.g., permission denied)
  const [error, setError] = useState<string | null>(null);

  // Load the directory listing whenever currentPath changes.
  // useEffect runs after the component renders, and re-runs when
  // the values in the dependency array [currentPath] change.
  useEffect(() => {
    let cancelled = false;  // Prevents stale responses from overwriting state

    const loadDir = async () => {
      setLoading(true);
      setError(null);
      const result = await listDirectory(currentPath);

      // If the component unmounted or path changed before the RPC returned,
      // discard this result to avoid showing stale data.
      if (cancelled) return;

      setCurrentPath(result.path);  // Use normalized path from backend
      setEntries(result.entries);
      setError(result.error);
      setLoading(false);
    };

    loadDir();

    // Cleanup function: runs if useEffect re-fires (path changed) or
    // component unmounts. Sets `cancelled = true` so the old loadDir()
    // call won't update state.
    return () => { cancelled = true; };
  }, [currentPath]);

  // Navigate up one directory level.
  // os.path.dirname("/home/deck") => "/home"
  const goUp = () => {
    const parent = currentPath.replace(/\/[^/]*\/?$/, "") || "/";
    setCurrentPath(parent);
  };

  return (
    <>
      {/* Section header: shows the current directory path */}
      <PanelSection title="Select Credentials File">
        {/* Current path display */}
        <PanelSectionRow>
          <Field label="Path">
            <div style={{
              fontSize: "12px",
              wordBreak: "break-all",      // Break long paths so they wrap
              color: "#b8bcbf",            // Steam's secondary text color
              padding: "4px 0"
            }}>
              {currentPath}
            </div>
          </Field>
        </PanelSectionRow>

        {/* Loading indicator */}
        {loading && (
          <PanelSectionRow>
            <div style={{ textAlign: "center", padding: "8px", color: "#b8bcbf" }}>
              Loading...
            </div>
          </PanelSectionRow>
        )}

        {/* Error display */}
        {error && (
          <PanelSectionRow>
            <div style={{ color: "#ff4444", padding: "4px 0", fontSize: "13px" }}>
              {error}
            </div>
          </PanelSectionRow>
        )}

        {/* Parent directory button — navigate up one level */}
        {!loading && (
          <PanelSectionRow>
            <ButtonItem layout="below" onClick={goUp}>
              <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                <FaArrowLeft size={14} />
                <span>.. (parent directory)</span>
              </div>
            </ButtonItem>
          </PanelSectionRow>
        )}

        {/* Directory entries — folders and .json files */}
        {!loading && entries.map((entry) => (
          <PanelSectionRow key={entry.name}>
            <ButtonItem
              layout="below"
              onClick={() => {
                const fullPath = currentPath.endsWith("/")
                  ? currentPath + entry.name
                  : currentPath + "/" + entry.name;

                if (entry.is_dir) {
                  // Navigate into the directory
                  setCurrentPath(fullPath);
                } else {
                  // User selected a .json file — trigger credential loading
                  onFileSelected(fullPath);
                }
              }}
            >
              <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                {/* Icon: folder or file */}
                {entry.is_dir
                  ? <FaFolder size={14} style={{ color: "#dcb867" }} />
                  : <FaFileAlt size={14} style={{ color: "#67b7dc" }} />
                }
                <span style={{ flex: 1 }}>{entry.name}</span>
                {/* Show file size for files (not directories) */}
                {!entry.is_dir && (
                  <span style={{ color: "#b8bcbf", fontSize: "12px" }}>
                    {formatSize(entry.size)}
                  </span>
                )}
              </div>
            </ButtonItem>
          </PanelSectionRow>
        ))}

        {/* Show a message if the directory is empty (no matching entries) */}
        {!loading && !error && entries.length === 0 && (
          <PanelSectionRow>
            <div style={{ textAlign: "center", padding: "8px", color: "#b8bcbf" }}>
              No folders or .json files found
            </div>
          </PanelSectionRow>
        )}
      </PanelSection>

      {/* Cancel button — returns to the normal settings view */}
      <PanelSection>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={onCancel}>
            Cancel
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    </>
  );
}


// =============================================================================
// Content component — the main plugin panel UI
// =============================================================================
// =============================================================================
// WordFilterModal — Full-screen modal for editing comma-separated word lists.
// =============================================================================
// TextField in the QAM panel doesn't receive keyboard focus properly (input
// goes to the wrong target, and the keyboard is partially covered by the panel).
// Opening a full-screen modal via showModal() fixes both issues — the modal
// takes over the screen so the keyboard has room and focus works correctly.
// Pattern from Decky-Translator's ApiKeyModal (TabTranslation.tsx:55-95).

function WordFilterModal({ title, description, currentValue, onSave, closeModal }: {
  title: string;
  description: string;
  currentValue: string;
  onSave: (value: string) => void;
  closeModal?: () => void;
}) {
  const [text, setText] = useState(currentValue || "");

  // Suppress touch gestures while the modal is open (same as keyboard suppression)
  useEffect(() => {
    setModalVisible(true);
    return () => { setModalVisible(false); };
  }, []);

  return (
    <ModalRoot onCancel={closeModal} onEscKeypress={closeModal}>
      <div style={{ padding: "20px", minWidth: "400px" }}>
        <h2 style={{ marginBottom: "15px" }}>{title}</h2>
        <p style={{ marginBottom: "15px", color: "#aaa", fontSize: "13px" }}>
          {description}
        </p>
        <TextField
          label="Words"
          value={text}
          bShowClearAction={true}
          onChange={(e: any) => setText(e.target.value)}
        />
        <Focusable
          style={{ display: "flex", gap: "10px", marginTop: "20px", justifyContent: "flex-end" }}
          flow-children="horizontal"
        >
          <DialogButton onClick={closeModal}>Cancel</DialogButton>
          <DialogButton onClick={() => { onSave(text); closeModal?.(); }}>
            Save
          </DialogButton>
        </Focusable>
      </div>
    </ModalRoot>
  );
}

// This is the top-level component rendered inside the Decky sidebar panel.
// It switches between two modes:
//   - Normal mode: shows settings, credential status, and action buttons
//   - File browser mode: shows the FileBrowser for selecting a JSON file

function Content({ overlayState }: { overlayState: OverlayState }) {
  // Phase 14: Track QAM visibility and notify backend so touch gestures
  // are suppressed while the "..." menu is open. useQuickAccessVisible()
  // listens to the QuickAccess window's visibilitychange DOM event.
  const isQamVisible = useQuickAccessVisible();
  useEffect(() => {
    setQamVisible(isQamVisible);
  }, [isQamVisible]);

  // Current plugin settings, loaded from the backend on mount
  const [settings, setSettings] = useState<PluginSettings | null>(null);
  // UI mode: "normal" shows settings, "browser" shows file picker
  const [mode, setMode] = useState<"normal" | "browser">("normal");
  // Status message shown after loading/clearing credentials
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  // Whether the status message is a success (green) or error (red)
  const [statusIsSuccess, setStatusIsSuccess] = useState(false);
  // Whether a credential file is currently being loaded
  const [loadingCreds, setLoadingCreds] = useState(false);

  // Ref for the volume save debounce timeout
  const volumeSaveTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Ref for region slider save debounce timeouts (one per setting key)
  const regionSaveTimeoutsRef = useRef<Record<string, ReturnType<typeof setTimeout> | null>>({});

  // --- Button monitor state (Phase 7: L4 Button Trigger) ---
  // Status of the hidraw button monitor (fetched on mount and after changes)
  const [monitorStatus, setMonitorStatus] = useState<ButtonMonitorStatus | null>(null);

  // --- Voice management state (Phase 8.6: On-Demand Voice Downloads) ---
  // Registry of all available Piper voices with download status
  const [localVoices, setLocalVoices] = useState<VoiceRegistry | null>(null);
  // Whether a voice download is in progress (disables buttons)
  const [isVoiceDownloading, setIsVoiceDownloading] = useState(false);
  // Status message from the last voice download/delete operation
  const [voiceMessage, setVoiceMessage] = useState<string | null>(null);
  // Whether the voice message is a success (green) or error (red)
  const [voiceIsSuccess, setVoiceIsSuccess] = useState(false);

  // --- Touchscreen state (Phase 9) ---
  // Status of the touchscreen monitor (fetched on mount and after mode changes)
  const [touchscreenStatus, setTouchscreenStatus] = useState<TouchscreenStatus | null>(null);

  // --- Region preview overlay state (Phase 13) ---
  // Whether the overlay is currently visible (synced from OverlayState)
  const [isOverlayVisible, setIsOverlayVisible] = useState(false);
  // Whether a screenshot is being captured for the overlay
  const [isOverlayLoading, setIsOverlayLoading] = useState(false);
  // Ref for the overlay auto-dismiss timeout (10s safety net)
  const overlayTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Ref for scrolling to the bottom of the Debug section after toggle (Phase 21)
  const debugEndRef = useRef<HTMLDivElement>(null);

  // Load settings, monitor status, and voice list from the backend when
  // the component first mounts. Also reload when returning from file browser mode.
  useEffect(() => {
    const loadSettings = async () => {
      const result = await getSettings();
      setSettings(result);
      // Also fetch button monitor status for the status indicator
      const status = await getButtonMonitorStatus();
      setMonitorStatus(status);
      // Fetch available voices with download status
      const voices = await getAvailableVoices();
      setLocalVoices(voices);
      // Fetch touchscreen monitor status
      const touchStatus = await getTouchscreenStatus();
      setTouchscreenStatus(touchStatus);
    };
    loadSettings();
  }, [mode]);  // Re-fetch settings when mode changes (e.g., after loading creds)

  // Handle the user selecting a .json file in the file browser.
  // Calls the backend to validate and store the credentials.
  const handleFileSelected = async (filePath: string) => {
    setLoadingCreds(true);
    const result = await loadCredentialsFile(filePath);
    setLoadingCreds(false);

    // Show the result message
    setStatusMessage(result.message);
    setStatusIsSuccess(result.valid);

    // Return to normal mode (settings view)
    setMode("normal");

    // Auto-clear the status message after 5 seconds
    setTimeout(() => setStatusMessage(null), 5000);
  };

  // Handle clearing stored credentials
  const handleClearCredentials = async () => {
    await clearCredentials();
    // Refresh settings to update the UI
    const updated = await getSettings();
    setSettings(updated);
    setStatusMessage("Credentials cleared");
    setStatusIsSuccess(true);
    setTimeout(() => setStatusMessage(null), 5000);
  };

  // Handle toggling a boolean setting (enabled, debug)
  const handleToggle = async (key: string, value: boolean) => {
    await saveSetting(key, value);
    // Update local state immediately for responsive UI
    if (settings) {
      setSettings({ ...settings, [key]: value });
    }
  };

  // Handle volume slider changes — update UI immediately, debounce save
  // to prevent rapid writes to disk during slider drag.
  const handleVolumeChange = (value: number) => {
    // Update UI immediately for responsive feel
    if (settings) {
      setSettings({ ...settings, volume: value });
    }

    // Debounce the save: clear any pending timeout, set a new one.
    // This way, the actual save only happens 800ms after the user stops dragging.
    if (volumeSaveTimeoutRef.current) {
      clearTimeout(volumeSaveTimeoutRef.current);
    }
    volumeSaveTimeoutRef.current = setTimeout(() => {
      saveSetting("volume", value);
    }, 800);
  };

  // Handle fixed region slider changes — update UI immediately, debounce save
  // to prevent rapid writes to disk during slider drag (same pattern as volume).
  const handleRegionChange = (key: string, value: number) => {
    if (settings) setSettings({ ...settings, [key]: value });
    if (regionSaveTimeoutsRef.current[key]) {
      clearTimeout(regionSaveTimeoutsRef.current[key]!);
    }
    regionSaveTimeoutsRef.current[key] = setTimeout(() => {
      saveSetting(key, value);
    }, 800);
  };

  // --- Voice management handlers (Phase 8.6) ---

  // Handle downloading a Piper voice model from HuggingFace
  const handleDownloadVoice = async (voiceId: string) => {
    setIsVoiceDownloading(true);
    setVoiceMessage(null);
    const result = await downloadVoice(voiceId);
    setIsVoiceDownloading(false);
    setVoiceMessage(result.message);
    setVoiceIsSuccess(result.success);
    // Refresh voice list to update download status
    const voices = await getAvailableVoices();
    setLocalVoices(voices);
    setTimeout(() => setVoiceMessage(null), 5000);
  };

  // Handle deleting a downloaded Piper voice model
  const handleDeleteVoice = async (voiceId: string) => {
    setVoiceMessage(null);
    const result = await deleteVoice(voiceId);
    setVoiceMessage(result.message);
    setVoiceIsSuccess(result.success);
    // Refresh voice list to update download status
    const voices = await getAvailableVoices();
    setLocalVoices(voices);
    setTimeout(() => setVoiceMessage(null), 5000);
  };

  // Phase 12: Whether the current capture mode needs touchscreen input.
  const needsTouch = settings?.capture_mode === "swipe_selection"
    || settings?.capture_mode === "two_tap_selection"
    || settings?.capture_mode === "hybrid";

  // Phase 13: Helper to mount/unmount the overlay global component.
  // When showing: register the component with routerHook so it renders
  // outside the QAM panel. When hiding: remove it completely so the
  // useUIComposition hook is destroyed and Gamescope input routing is restored.
  const showOverlayComponent = () => {
    routerHook.addGlobalComponent("DCRRegionPreview", () => (
      <RegionPreviewOverlay state={overlayState} />
    ));
    setIsOverlayVisible(true);
    // Auto-dismiss after 10 seconds to ensure the composition layer
    // doesn't stay alive indefinitely (e.g., if user forgets to close it)
    if (overlayTimeoutRef.current) clearTimeout(overlayTimeoutRef.current);
    overlayTimeoutRef.current = setTimeout(() => {
      hideOverlayComponent();
    }, 10000);
  };

  const hideOverlayComponent = () => {
    if (overlayTimeoutRef.current) {
      clearTimeout(overlayTimeoutRef.current);
      overlayTimeoutRef.current = null;
    }
    routerHook.removeGlobalComponent("DCRRegionPreview");
    overlayState.hide();
    setIsOverlayVisible(false);
  };

  // Cleanup: clear timeouts and remove overlay when the component
  // unmounts. This handles both QAM close and switching to another plugin tab.
  useEffect(() => {
    return () => {
      if (volumeSaveTimeoutRef.current) {
        clearTimeout(volumeSaveTimeoutRef.current);
      }
      for (const key of Object.keys(regionSaveTimeoutsRef.current)) {
        if (regionSaveTimeoutsRef.current[key]) {
          clearTimeout(regionSaveTimeoutsRef.current[key]!);
        }
      }
      // Phase 13: Remove overlay component when leaving the plugin panel
      if (overlayTimeoutRef.current) {
        clearTimeout(overlayTimeoutRef.current);
        overlayTimeoutRef.current = null;
      }
      routerHook.removeGlobalComponent("DCRRegionPreview");
      overlayState.hide();
      // Phase 14: Reset QAM visibility flag on unmount — the useEffect for
      // isQamVisible may not fire before the component unmounts, so we
      // explicitly clear it to avoid leaving touch gestures suppressed.
      setQamVisible(false);
    };
  }, []);

  // --- File browser mode ---
  if (mode === "browser") {
    return (
      <FileBrowser
        onFileSelected={handleFileSelected}
        onCancel={() => setMode("normal")}
      />
    );
  }

  // --- Loading state (settings not yet fetched) ---
  if (!settings) {
    return (
      <PanelSection title="Cloud Reader">
        <PanelSectionRow>
          <div style={{ textAlign: "center", padding: "8px", color: "#b8bcbf" }}>
            Loading...
          </div>
        </PanelSectionRow>
      </PanelSection>
    );
  }

  // Check if either provider uses GCP (controls GCP Credentials section visibility)
  const needsGcp = settings.ocr_provider === "gcp" || settings.tts_provider === "gcp";

  // --- Normal mode (settings view) ---
  return (
    <>
      {/* ---- Enabled Section ---- */}
      {/* Master on/off toggle at the very top for quick access */}
      <PanelSection title="Cloud Reader">
        <PanelSectionRow>
          <ToggleField
            label="Enabled"
            description="Master switch — disables triggers and OCR/TTS"
            checked={settings.enabled}
            onChange={(value) => handleToggle("enabled", value)}
          />
        </PanelSectionRow>
      </PanelSection>

      {/* ---- Button Trigger Section (Phase 7) ---- */}
      {/* Configures which hardware button triggers the Read Screen pipeline
          without opening the Decky panel. This is the key UX feature for
          in-game use — press-and-hold a back button to hear screen text. */}
      <PanelSection title="Button Trigger">
        {/* Button selection dropdown */}
        <PanelSectionRow>
          <DropdownItem
            label="Trigger Button"
            description="Hold to trigger Read Screen"
            menuLabel="Select Button"
            rgOptions={TRIGGER_BUTTON_OPTIONS.map((o) => ({
              data: o.data,
              label: o.label,
            }))}
            selectedOption={
              TRIGGER_BUTTON_OPTIONS.find((o) => o.data === settings.trigger_button)?.data
              ?? TRIGGER_BUTTON_OPTIONS[0].data  // Default: disabled
            }
            onChange={async (option) => {
              await saveSetting("trigger_button", option.data);
              if (settings) {
                setSettings({ ...settings, trigger_button: option.data as string });
              }
              // Re-fetch monitor status after changing the button
              const status = await getButtonMonitorStatus();
              setMonitorStatus(status);
            }}
          />
        </PanelSectionRow>

        {/* Hold time dropdown — only shown when trigger is not disabled */}
        {settings.trigger_button !== "disabled" && (
          <PanelSectionRow>
            <DropdownItem
              label="Hold Time"
              description="How long to hold before triggering"
              menuLabel="Select Hold Time"
              rgOptions={HOLD_TIME_OPTIONS.map((o) => ({
                data: o.data,
                label: o.label,
              }))}
              selectedOption={
                HOLD_TIME_OPTIONS.find((o) => o.data === settings.hold_time_ms)?.data
                ?? HOLD_TIME_OPTIONS[1].data  // Default: 500ms
              }
              onChange={async (option) => {
                await saveSetting("hold_time_ms", option.data);
                if (settings) {
                  setSettings({ ...settings, hold_time_ms: option.data as number });
                }
                // Re-fetch monitor status after changing hold time
                const status = await getButtonMonitorStatus();
                setMonitorStatus(status);
              }}
            />
          </PanelSectionRow>
        )}

        {/* Hint text explaining what the button trigger does */}
        <PanelSectionRow>
          <div style={{
            color: "#b8bcbf",
            fontSize: "12px",
            padding: "4px 0",
          }}>
            {settings.trigger_button === "disabled"
              ? "Enable a button to trigger Read Screen without opening this panel"
              : settings.hold_time_ms === 0
                ? `Press ${settings.trigger_button} to trigger Read Screen`
                : `Hold ${settings.trigger_button} for ${settings.hold_time_ms}ms to trigger Read Screen`
            }
          </div>
        </PanelSectionRow>
      </PanelSection>

      {/* ---- Capture Mode Section (Phase 12) ---- */}
      {/* Configures how the screen region is selected for OCR. Modes range from
          full-screen (button only) to interactive touchscreen selection. The
          touchscreen monitor is auto-managed based on the selected mode. */}
      <PanelSection title="Capture Mode">
        {/* Mode selection dropdown */}
        <PanelSectionRow>
          <DropdownItem
            label="Capture Mode"
            description="How the OCR region is selected"
            menuLabel="Select Capture Mode"
            rgOptions={CAPTURE_MODE_OPTIONS.map((o) => ({
              data: o.data,
              label: o.label,
            }))}
            selectedOption={
              CAPTURE_MODE_OPTIONS.find((o) => o.data === settings.capture_mode)?.data
              ?? CAPTURE_MODE_OPTIONS[0].data
            }
            onChange={async (option) => {
              await saveSetting("capture_mode", option.data);
              if (settings) {
                setSettings({ ...settings, capture_mode: option.data as string });
              }
              // Fetch touchscreen status after mode change (auto-managed)
              setTimeout(async () => {
                const status = await getTouchscreenStatus();
                setTouchscreenStatus(status);
              }, 500);
            }}
          />
        </PanelSectionRow>

        {/* Mode description — explains how the selected mode works */}
        <PanelSectionRow>
          <div style={{ color: "#b8bcbf", fontSize: "12px", padding: "4px 0" }}>
            {settings.capture_mode === "full_screen"
              ? `Press ${settings.trigger_button !== "disabled" ? settings.trigger_button : "trigger button"} to capture entire screen`
              : settings.capture_mode === "swipe_selection"
              ? "Swipe on screen to select a region for OCR"
              : settings.capture_mode === "two_tap_selection"
              ? "Tap two corners to define a rectangle for OCR"
              : settings.capture_mode === "fixed_region"
              ? `Press ${settings.trigger_button !== "disabled" ? settings.trigger_button : "trigger button"} to capture the configured region`
              : settings.capture_mode === "hybrid"
              ? `Press ${settings.trigger_button !== "disabled" ? settings.trigger_button : "trigger button"} for fixed region, or tap for two-tap selection`
              : ""
            }
          </div>
        </PanelSectionRow>

        {/* Fixed region configuration — shown for fixed_region and hybrid modes */}
        {(settings.capture_mode === "fixed_region" || settings.capture_mode === "hybrid") && (
          <>
            {/* Current fixed region coordinates display */}
            <PanelSectionRow>
              <Field label="Fixed Region">
                <div style={{ color: "#67b7dc", fontSize: "13px" }}>
                  ({settings.fixed_region_x1}, {settings.fixed_region_y1}) - ({settings.fixed_region_x2}, {settings.fixed_region_y2})
                </div>
              </Field>
            </PanelSectionRow>

            {/* X1 slider */}
            <PanelSectionRow>
              <SliderField
                label="Left X"
                value={settings.fixed_region_x1}
                min={0}
                max={1280}
                step={10}
                onChange={(value: number) => handleRegionChange("fixed_region_x1", value)}
              />
            </PanelSectionRow>

            {/* Y1 slider */}
            <PanelSectionRow>
              <SliderField
                label="Top Y"
                value={settings.fixed_region_y1}
                min={0}
                max={800}
                step={10}
                onChange={(value: number) => handleRegionChange("fixed_region_y1", value)}
              />
            </PanelSectionRow>

            {/* X2 slider */}
            <PanelSectionRow>
              <SliderField
                label="Right X"
                value={settings.fixed_region_x2}
                min={0}
                max={1280}
                step={10}
                onChange={(value: number) => handleRegionChange("fixed_region_x2", value)}
              />
            </PanelSectionRow>

            {/* Y2 slider */}
            <PanelSectionRow>
              <SliderField
                label="Bottom Y"
                value={settings.fixed_region_y2}
                min={0}
                max={800}
                step={10}
                onChange={(value: number) => handleRegionChange("fixed_region_y2", value)}
              />
            </PanelSectionRow>

            {/* Apply Last Selection button */}
            <PanelSectionRow>
              <ButtonItem
                layout="below"
                onClick={async () => {
                  const result = await applyLastSelectionToFixedRegion();
                  if (result.success) {
                    // Refresh settings to show updated fixed region
                    const updated = await getSettings();
                    setSettings(updated);
                  }
                }}
              >
                Apply Last Selection
              </ButtonItem>
            </PanelSectionRow>

            {/* Phase 13: Region Preview toggle button.
                Captures a fresh screenshot and shows it with the fixed region
                highlighted, to the left of the QAM panel. Toggle off to hide. */}
            <PanelSectionRow>
              <ButtonItem
                layout="below"
                disabled={isOverlayLoading}
                onClick={async () => {
                  if (isOverlayVisible) {
                    // Toggle off — unmount the overlay component entirely
                    hideOverlayComponent();
                  } else {
                    // Toggle on — capture a fresh screenshot, set state, then mount
                    setIsOverlayLoading(true);
                    const result = await captureOverlayScreenshot();
                    setIsOverlayLoading(false);
                    if (result.success) {
                      // Set the data on OverlayState first (component reads on mount)
                      overlayState.show(result.image_base64, {
                        x1: settings.fixed_region_x1,
                        y1: settings.fixed_region_y1,
                        x2: settings.fixed_region_x2,
                        y2: settings.fixed_region_y2,
                      });
                      // Then mount the global component
                      showOverlayComponent();
                    }
                  }
                }}
              >
                {isOverlayLoading
                  ? "Capturing..."
                  : isOverlayVisible
                  ? "Hide Region Preview"
                  : "Show Region Preview"
                }
              </ButtonItem>
            </PanelSectionRow>

            {/* Last selection coordinates reference */}
            <PanelSectionRow>
              <Field label="Last Selection">
                <div style={{ color: "#b8bcbf", fontSize: "12px" }}>
                  ({settings.last_selection_x1}, {settings.last_selection_y1}) - ({settings.last_selection_x2}, {settings.last_selection_y2})
                </div>
              </Field>
            </PanelSectionRow>
          </>
        )}

      </PanelSection>

      {/* ---- Provider Section (Phase 8) ---- */}
      {/* Lets the user choose between Google Cloud (online) and local (offline)
          for OCR and TTS independently. Changing a provider stops the old
          worker so the new one lazy-starts on next use. */}
      <PanelSection title="Provider">
        {/* OCR Engine dropdown */}
        <PanelSectionRow>
          <DropdownItem
            label="OCR Engine"
            description="Text detection engine"
            menuLabel="Select OCR Engine"
            rgOptions={OCR_PROVIDER_OPTIONS.map((o) => ({
              data: o.data,
              label: o.label,
            }))}
            selectedOption={
              OCR_PROVIDER_OPTIONS.find((o) => o.data === settings.ocr_provider)?.data
              ?? OCR_PROVIDER_OPTIONS[0].data
            }
            onChange={async (option) => {
              await saveSetting("ocr_provider", option.data);
              const updated = await getSettings();
              setSettings(updated);
            }}
          />
        </PanelSectionRow>

        {/* TTS Engine dropdown */}
        <PanelSectionRow>
          <DropdownItem
            label="TTS Engine"
            description="Speech synthesis engine"
            menuLabel="Select TTS Engine"
            rgOptions={TTS_PROVIDER_OPTIONS.map((o) => ({
              data: o.data,
              label: o.label,
            }))}
            selectedOption={
              TTS_PROVIDER_OPTIONS.find((o) => o.data === settings.tts_provider)?.data
              ?? TTS_PROVIDER_OPTIONS[0].data
            }
            onChange={async (option) => {
              await saveSetting("tts_provider", option.data);
              const updated = await getSettings();
              setSettings(updated);
            }}
          />
        </PanelSectionRow>

        {/* Provider status hints */}
        {settings.ocr_provider === "local" && !settings.is_local_available && (
          <PanelSectionRow>
            <div style={{ color: "#e74c3c", fontSize: "12px", padding: "4px 0" }}>
              Local OCR unavailable — bundled Python not found
            </div>
          </PanelSectionRow>
        )}
        {settings.tts_provider === "local" && !settings.is_local_available && (
          <PanelSectionRow>
            <div style={{ color: "#e74c3c", fontSize: "12px", padding: "4px 0" }}>
              Local TTS unavailable — bundled Python not found
            </div>
          </PanelSectionRow>
        )}
        {settings.ocr_provider === "gcp" && !settings.is_gcp_configured && (
          <PanelSectionRow>
            <div style={{ color: "#e74c3c", fontSize: "12px", padding: "4px 0" }}>
              GCP OCR needs credentials — load them below
            </div>
          </PanelSectionRow>
        )}
        {settings.tts_provider === "gcp" && !settings.is_gcp_configured && (
          <PanelSectionRow>
            <div style={{ color: "#e74c3c", fontSize: "12px", padding: "4px 0" }}>
              GCP TTS needs credentials — load them below
            </div>
          </PanelSectionRow>
        )}
      </PanelSection>

      {/* ---- GCP Credentials Section ---- */}
      {/* Only shown when at least one provider uses GCP */}
      {needsGcp && <PanelSection title="GCP Credentials">
        {/* Status: Configured or Not Configured */}
        <PanelSectionRow>
          <Field label="Status">
            <div style={{
              color: settings.is_gcp_configured ? "#2ecc71" : "#e74c3c",
              fontWeight: "bold"
            }}>
              {settings.is_gcp_configured ? "Configured" : "Not Configured"}
            </div>
          </Field>
        </PanelSectionRow>

        {/* Show project ID when credentials are loaded */}
        {settings.is_gcp_configured && settings.project_id && (
          <PanelSectionRow>
            <Field label="Project">
              <div style={{ color: "#b8bcbf", fontSize: "13px" }}>
                {settings.project_id}
              </div>
            </Field>
          </PanelSectionRow>
        )}

        {/* Status message (success/error) shown after credential operations */}
        {statusMessage && (
          <PanelSectionRow>
            <div style={{
              color: statusIsSuccess ? "#2ecc71" : "#e74c3c",
              padding: "4px 0",
              fontSize: "13px"
            }}>
              {statusMessage}
            </div>
          </PanelSectionRow>
        )}

        {/* Loading indicator while credentials are being validated */}
        {loadingCreds && (
          <PanelSectionRow>
            <div style={{ textAlign: "center", padding: "4px", color: "#b8bcbf" }}>
              Loading credentials...
            </div>
          </PanelSectionRow>
        )}

        {/* Load Credentials button — opens the file browser */}
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => setMode("browser")}>
            {settings.is_gcp_configured ? "Change Credentials" : "Load Credentials"}
          </ButtonItem>
        </PanelSectionRow>

        {/* Clear Credentials button — only shown when credentials are loaded */}
        {settings.is_gcp_configured && (
          <PanelSectionRow>
            <ButtonItem layout="below" onClick={handleClearCredentials}>
              Clear Credentials
            </ButtonItem>
          </PanelSectionRow>
        )}
      </PanelSection>}

      {/* ---- Sound Effects Section (Phase 11) ---- */}
      {/* UI feedback sounds for capture mode interactions. Sounds play
          independently of TTS (fire-and-forget). Mute toggle respects
          the mute_interface_sounds setting. */}
      <PanelSection title="Sound Effects">
        {/* Mute interface sounds toggle */}
        <PanelSectionRow>
          <ToggleField
            label="Mute Interface Sounds"
            description="Disable UI feedback sounds"
            checked={settings.mute_interface_sounds}
            onChange={(value) => handleToggle("mute_interface_sounds", value)}
          />
        </PanelSectionRow>
      </PanelSection>

      {/* ---- Text Filtering Section (Phase 14) ---- */}
      {/* Configurable word filters applied between OCR and TTS in the pipeline.
          Two modes: "Always" removes words anywhere, "Beginning" removes words
          from the first N tokens only. Each mode has its own enable toggle. */}
      <PanelSection title="Text Filtering">
        {/* "Always" filter toggle — remove specified words anywhere in OCR text */}
        <PanelSectionRow>
          <ToggleField
            label="Filter Words (Always)"
            description="Remove specified words anywhere in detected text"
            checked={settings.ignored_words_always_enabled}
            onChange={(value) => handleToggle("ignored_words_always_enabled", value)}
          />
        </PanelSectionRow>

        {/* Button to open modal for editing "always" word list. Uses a full-screen
            modal so the on-screen keyboard gets proper focus and isn't covered. */}
        {settings.ignored_words_always_enabled && (
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              description={settings.ignored_words_always || "(none)"}
              onClick={() => {
                showModal(
                  <WordFilterModal
                    title="Filter Words (Always)"
                    description="Enter comma-separated words to remove anywhere in detected text (e.g. word1, word2)"
                    currentValue={settings.ignored_words_always}
                    onSave={(value) => {
                      saveSetting("ignored_words_always", value);
                      if (settings) setSettings({ ...settings, ignored_words_always: value });
                    }}
                  />
                );
              }}
            >
              Edit Word List
            </ButtonItem>
          </PanelSectionRow>
        )}

        {/* "Beginning" filter toggle — remove specified words from start of text */}
        <PanelSectionRow>
          <ToggleField
            label="Filter Words (Beginning)"
            description="Remove specified words from the start of detected text"
            checked={settings.ignored_words_beginning_enabled}
            onChange={(value) => handleToggle("ignored_words_beginning_enabled", value)}
          />
        </PanelSectionRow>

        {/* Button to open modal for editing "beginning" word list + word count slider */}
        {settings.ignored_words_beginning_enabled && (
          <>
            <PanelSectionRow>
              <ButtonItem
                layout="below"
                description={settings.ignored_words_beginning || "(none)"}
                onClick={() => {
                  showModal(
                    <WordFilterModal
                      title="Filter Words (Beginning)"
                      description="Enter comma-separated words to remove from the start of detected text (e.g. Chapter, Narrator)"
                      currentValue={settings.ignored_words_beginning}
                      onSave={(value) => {
                        saveSetting("ignored_words_beginning", value);
                        if (settings) setSettings({ ...settings, ignored_words_beginning: value });
                      }}
                    />
                  );
                }}
              >
                Edit Word List
              </ButtonItem>
            </PanelSectionRow>

            {/* How many leading words to check for the "beginning" filter */}
            <PanelSectionRow>
              <SliderField
                label={`Words to Check: ${settings.ignored_words_count}`}
                description="Number of leading words to scan for matches"
                value={settings.ignored_words_count}
                min={1}
                max={10}
                step={1}
                onChange={(value: number) => handleRegionChange("ignored_words_count", value)}
              />
            </PanelSectionRow>
          </>
        )}
      </PanelSection>

      {/* ---- TTS (Text-to-Speech) Section ---- */}
      <PanelSection title="Text-to-Speech">
        {/* Voice selection — different options based on TTS provider */}
        {settings.tts_provider === "gcp" ? (
          <>
            {/* GCP Voice selection dropdown */}
            <PanelSectionRow>
              <DropdownItem
                label="Voice"
                description="Neural2 voice for speech synthesis"
                menuLabel="Select Voice"
                rgOptions={VOICE_OPTIONS.map((v) => ({
                  data: v.data,
                  label: v.label,
                }))}
                selectedOption={
                  VOICE_OPTIONS.find((v) => v.data === settings.voice_id)?.data
                  ?? VOICE_OPTIONS[0].data
                }
                onChange={(option) => {
                  saveSetting("voice_id", option.data);
                  if (settings) {
                    setSettings({ ...settings, voice_id: option.data as string });
                  }
                }}
              />
            </PanelSectionRow>

            {/* GCP Speech rate dropdown */}
            <PanelSectionRow>
              <DropdownItem
                label="Speech Rate"
                description="How fast the text is read aloud"
                menuLabel="Select Speed"
                rgOptions={SPEECH_RATE_OPTIONS.map((r) => ({
                  data: r.data,
                  label: r.label,
                }))}
                selectedOption={
                  SPEECH_RATE_OPTIONS.find((r) => r.data === settings.speech_rate)?.data
                  ?? SPEECH_RATE_OPTIONS[2].data
                }
                onChange={(option) => {
                  saveSetting("speech_rate", option.data);
                  if (settings) {
                    setSettings({ ...settings, speech_rate: option.data as string });
                  }
                }}
              />
            </PanelSectionRow>
          </>
        ) : (
          <>
            {/* Local (Piper) Voice selection dropdown — populated from backend */}
            <PanelSectionRow>
              <DropdownItem
                label="Voice"
                description="Piper voice for offline speech synthesis"
                menuLabel="Select Voice"
                rgOptions={
                  localVoices
                    ? Object.entries(localVoices).map(([id, info]) => ({
                        data: id,
                        label: info.downloaded
                          ? info.label
                          : `${info.label} [~63 MB]`,
                      }))
                    : [{ data: settings.local_voice_id, label: "Loading..." }]
                }
                selectedOption={settings.local_voice_id}
                onChange={(option) => {
                  saveSetting("local_voice_id", option.data);
                  if (settings) {
                    setSettings({ ...settings, local_voice_id: option.data as string });
                  }
                }}
              />
            </PanelSectionRow>

            {/* Voice download/delete buttons and status */}
            {localVoices && (() => {
              const selectedVoice = localVoices[settings.local_voice_id];
              const isDownloaded = selectedVoice?.downloaded ?? false;
              return (
                <>
                  {/* Voice status message */}
                  {voiceMessage && (
                    <PanelSectionRow>
                      <div style={{
                        color: voiceIsSuccess ? "#2ecc71" : "#e74c3c",
                        padding: "4px 0",
                        fontSize: "13px"
                      }}>
                        {voiceMessage}
                      </div>
                    </PanelSectionRow>
                  )}

                  {isDownloaded ? (
                    <>
                      {/* Show downloaded indicator + file size */}
                      <PanelSectionRow>
                        <div style={{
                          color: "#2ecc71",
                          fontSize: "12px",
                          padding: "4px 0"
                        }}>
                          Downloaded ({formatSize(selectedVoice.file_size)})
                        </div>
                      </PanelSectionRow>
                      {/* Delete button */}
                      <PanelSectionRow>
                        <ButtonItem
                          layout="below"
                          onClick={() => handleDeleteVoice(settings.local_voice_id)}
                        >
                          Delete Voice
                        </ButtonItem>
                      </PanelSectionRow>
                    </>
                  ) : (
                    <>
                      {/* Not downloaded hint */}
                      <PanelSectionRow>
                        <div style={{
                          color: "#b8bcbf",
                          fontSize: "12px",
                          padding: "4px 0"
                        }}>
                          Voice will auto-download on first use
                        </div>
                      </PanelSectionRow>
                      {/* Download button */}
                      <PanelSectionRow>
                        <ButtonItem
                          layout="below"
                          onClick={() => handleDownloadVoice(settings.local_voice_id)}
                          disabled={isVoiceDownloading}
                        >
                          {isVoiceDownloading ? "Downloading..." : "Download Voice"}
                        </ButtonItem>
                      </PanelSectionRow>
                    </>
                  )}
                </>
              );
            })()}

            {/* Local Speech rate dropdown */}
            <PanelSectionRow>
              <DropdownItem
                label="Speech Rate"
                description="How fast the text is read aloud"
                menuLabel="Select Speed"
                rgOptions={LOCAL_SPEECH_RATE_OPTIONS.map((r) => ({
                  data: r.data,
                  label: r.label,
                }))}
                selectedOption={
                  LOCAL_SPEECH_RATE_OPTIONS.find((r) => r.data === settings.local_speech_rate)?.data
                  ?? LOCAL_SPEECH_RATE_OPTIONS[2].data
                }
                onChange={(option) => {
                  saveSetting("local_speech_rate", option.data);
                  if (settings) {
                    setSettings({ ...settings, local_speech_rate: option.data as string });
                  }
                }}
              />
            </PanelSectionRow>
          </>
        )}

        {/* Volume slider (0-100, step 10, debounced save) */}
        <PanelSectionRow>
          <SliderField
            label="Volume"
            description="Audio playback volume"
            value={settings.volume}
            min={0}
            max={100}
            step={10}
            notchCount={3}
            notchLabels={[
              { notchIndex: 0, label: "0" },
              { notchIndex: 1, label: "50" },
              { notchIndex: 2, label: "100" },
            ]}
            onChange={handleVolumeChange}
          />
        </PanelSectionRow>

      </PanelSection>

      {/* ---- Debug Section ---- */}
      <PanelSection title="Debug">
        <PanelSectionRow>
          <ToggleField
            label="Debug Mode"
            description="Show extra diagnostic logging"
            checked={settings.debug}
            onChange={(value) => {
              handleToggle("debug", value);
              // Scroll the debug section's bottom into view after the indicators
              // appear, so the QAM scroll container reveals the new content (Phase 21).
              if (value) {
                setTimeout(() => debugEndRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" }), 100);
              }
            }}
          />
        </PanelSectionRow>

        {/* Monitor status indicators — only visible when Debug Mode is ON (Phase 21) */}
        {settings.debug && monitorStatus && settings.trigger_button !== "disabled" && (
          <PanelSectionRow>
            <Field label="Button Monitor">
              <div style={{
                color: monitorStatus.initialized ? "#2ecc71" : "#e74c3c",
                fontWeight: "bold",
                fontSize: "13px",
              }}>
                {monitorStatus.initialized ? "Connected" : "Not connected"}
              </div>
            </Field>
          </PanelSectionRow>
        )}
        {settings.debug && touchscreenStatus && needsTouch && (
          <PanelSectionRow>
            <Field label="Touchscreen">
              <div style={{
                color: touchscreenStatus.initialized ? "#2ecc71" : "#e74c3c",
                fontWeight: "bold",
                fontSize: "13px",
              }}>
                {touchscreenStatus.initialized ? "Connected" : "Not connected"}
              </div>
            </Field>
          </PanelSectionRow>
        )}
        {/* Scroll anchor — scrollIntoView target when debug is toggled ON */}
        {settings.debug && <div ref={debugEndRef} />}
      </PanelSection>

      {/* ---- Version Footer (Phase 19) ---- */}
      <div style={{ textAlign: "center", fontSize: "11px", color: "#666", padding: "8px 0" }}>
        Plugin v{PLUGIN_VERSION}
      </div>
      {/* Invisible spacer so gamepad D-pad navigation can reach the bottom */}
      <PanelSectionRow>
        <Focusable style={{ height: "1px", opacity: 0 }} onActivate={() => {}} />
      </PanelSectionRow>
    </>
  );
}


// =============================================================================
// Plugin registration
// =============================================================================
//
// definePlugin() is the entry point that Decky Loader calls when loading the
// plugin. It returns an object describing:
//   - name: shown in plugin lists and menus
//   - titleView: React element shown at the top of the plugin's sidebar panel
//   - content: React element for the panel body
//   - icon: React element for the plugin list icon
//   - onDismount: cleanup function called when the plugin unloads

export default definePlugin(() => {
  // This runs once when the plugin is first loaded on the frontend
  console.log("Decky Cloud Reader: frontend plugin loaded");

  // Phase 13: Create the shared overlay state instance.
  // This single instance is shared between the Content component (toggle button)
  // and the RegionPreviewOverlay global component (rendering).
  // The global component is NOT registered here — it's mounted/unmounted
  // on-demand by the Content component's toggle handler. This avoids keeping
  // a useUIComposition hook alive which would interfere with Gamescope input.
  const overlayState = new OverlayState();

  // Phase 14: Register for on-screen keyboard open/close events.
  // When the virtual keyboard is visible, the backend suppresses touch gestures
  // (two-tap, swipe) to avoid accidental OCR triggers while typing.
  let keyboardUnregister: { Unregister: () => void } | null = null;
  if (VIRTUAL_KEYBOARD_MANAGER) {
    try {
      keyboardUnregister = VIRTUAL_KEYBOARD_MANAGER
        .m_bIsInlineVirtualKeyboardOpen
        .m_callbacks
        .Register((isOpen: boolean) => {
          console.log(`Decky Cloud Reader: keyboard visible = ${isOpen}`);
          setKeyboardVisible(isOpen);
        });
      console.log("Decky Cloud Reader: VirtualKeyboardManager callback registered");
    } catch (e) {
      console.error("Decky Cloud Reader: failed to register keyboard callback", e);
    }
  } else {
    console.warn("Decky Cloud Reader: VirtualKeyboardManager not found — keyboard suppression disabled");
  }

  return {
    // The name shown in the Decky plugin list
    name: "Cloud Reader",

    // The styled title at the top of the plugin's sidebar panel
    titleView: (
      <div className={staticClasses.Title}>Cloud Reader</div>
    ),

    // The main content of the plugin panel, with overlay state for the toggle
    content: <Content overlayState={overlayState} />,

    // The icon shown in the plugin list sidebar (book icon)
    icon: <FaBook />,

    // Called when the plugin is unloaded (e.g., Decky Loader restarts)
    onDismount() {
      console.log("Decky Cloud Reader: frontend plugin unloaded");
      // Phase 13: Clean up overlay on plugin unload
      overlayState.hide();
      routerHook.removeGlobalComponent("DCRRegionPreview");
      // Phase 14: Unregister keyboard visibility callback
      keyboardUnregister?.Unregister();
      keyboardUnregister = null;
    },
  };
});
