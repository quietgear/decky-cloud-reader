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
  staticClasses     // CSS class names for standard Steam UI styling
} from "@decky/ui";

import {
  callable,         // Creates a typed function that calls a Python backend method
  definePlugin      // Registers this module as a Decky plugin
} from "@decky/api";

import { useState, useEffect, useRef } from "react";

// FaBook icon — fits the "reader" theme of this plugin.
// FaFolder/FaFile icons — used in the file browser for visual clarity.
import { FaBook, FaFolder, FaFileAlt, FaArrowLeft, FaVolumeUp, FaStop } from "react-icons/fa";


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

// Response from the capture_screenshot() backend RPC
interface CaptureResult {
  success: boolean;    // true if screenshot was captured successfully
  file_size: number;   // Size of the captured PNG in bytes
  message: string;     // Human-readable success or error message
}

// Response from the perform_ocr() backend RPC
interface OcrResult {
  success: boolean;    // true if OCR completed successfully
  text: string;        // Detected text (empty string if none found)
  char_count: number;  // Number of characters detected
  line_count: number;  // Number of lines detected
  message: string;     // Human-readable success or error message
}

// Response from the perform_tts() backend RPC
interface TtsResult {
  success: boolean;    // true if TTS synthesis + playback started
  message: string;     // Human-readable success or error message
  audio_size: number;  // Size of the synthesized MP3 in bytes
}

// Response from the stop_playback() backend RPC
interface StopResult {
  success: boolean;    // Always true (stop is best-effort)
  message: string;     // Human-readable message
}

// Response from the get_playback_status() backend RPC
interface PlaybackStatus {
  is_playing: boolean;  // true if mpv is currently playing audio
}

// Response from the read_screen() backend RPC (Phase 6 pipeline)
interface ReadScreenResult {
  success: boolean;    // true if the full pipeline completed successfully
  message: string;     // Human-readable success or error message
  step: string;        // Pipeline step where it finished (or failed)
  text: string;        // OCR text (populated even if TTS fails)
  audio_size: number;  // Size of the synthesized MP3 in bytes
}

// Response from the get_pipeline_status() backend RPC (Phase 6 polling)
interface PipelineStatus {
  running: boolean;    // true if the pipeline is currently running
  step: string;        // Current step: idle/capturing/ocr/tts/playing
  is_playing: boolean; // true if audio is currently playing
}

// Response from the stop_pipeline() backend RPC
interface StopPipelineResult {
  success: boolean;    // Always true (stop is best-effort)
  message: string;     // Human-readable message
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

// Capture a screenshot via GStreamer + PipeWire
const captureScreenshot = callable<[], CaptureResult>("capture_screenshot");

// Perform OCR on a fresh screenshot (capture + Cloud Vision API)
const performOcr = callable<[], OcrResult>("perform_ocr");

// Synthesize speech from text and start playback
const performTts = callable<[string], TtsResult>("perform_tts");

// Stop current audio playback
const stopPlayback = callable<[], StopResult>("stop_playback");

// Check if audio is currently playing (lightweight poll)
const getPlaybackStatus = callable<[], PlaybackStatus>("get_playback_status");

// Phase 6: End-to-end pipeline (capture → OCR → TTS → playback)
const readScreen = callable<[], ReadScreenResult>("read_screen");

// Cancel the running pipeline and stop playback
const stopPipeline = callable<[], StopPipelineResult>("stop_pipeline");

// Lightweight poll for pipeline progress (step, running, is_playing)
const getPipelineStatus = callable<[], PipelineStatus>("get_pipeline_status");

// Phase 7: Get button monitor status (running, device_path, error_count, etc.)
const getButtonMonitorStatus = callable<[], ButtonMonitorStatus>("get_button_monitor_status");

// Phase 8.6: Voice management — on-demand Piper voice downloads
const getAvailableVoices = callable<[], VoiceRegistry>("get_available_voices");
const downloadVoice = callable<[string], VoiceActionResult>("download_voice");
const deleteVoice = callable<[string], VoiceActionResult>("delete_voice");

// Phase 9: Touchscreen monitor status and last touch coordinates
const getTouchscreenStatus = callable<[], TouchscreenStatus>("get_touchscreen_status");
const getLastTouch = callable<[], { x: number; y: number } | null>("get_last_touch");


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
// Voice and speech rate options for the TTS dropdown selectors
// =============================================================================
// These match the VOICE_REGISTRY and SPEECH_RATE_MAP in gcp_worker.py.
// Each option has a `data` value (sent to the backend) and a `label` (shown in UI).

const VOICE_OPTIONS = [
  { data: "en-US-Neural2-A", label: "US English - Male A" },
  { data: "en-US-Neural2-C", label: "US English - Female C" },
  { data: "en-US-Neural2-D", label: "US English - Male D" },
  { data: "en-US-Neural2-F", label: "US English - Female F" },
  { data: "en-GB-Neural2-A", label: "UK English - Female A" },
  { data: "en-GB-Neural2-B", label: "UK English - Male B" },
  { data: "en-GB-Neural2-C", label: "UK English - Female C" },
  { data: "en-GB-Neural2-D", label: "UK English - Male D" },
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

const HOLD_TIME_OPTIONS = [
  { data: 300,  label: "300ms (Quick)" },
  { data: 500,  label: "500ms (Default)" },
  { data: 750,  label: "750ms" },
  { data: 1000, label: "1000ms (Long)" },
  { data: 1500, label: "1500ms (Very Long)" },
];


// =============================================================================
// Helper: map pipeline step strings to user-friendly labels
// =============================================================================
// Shown in the UI while the Read Screen pipeline is running, so the user
// knows what's happening. Each label uses present progressive tense.
function getPipelineStepLabel(step: string): string {
  switch (step) {
    case "starting":     return "Starting...";
    case "capturing":    return "Capturing screen...";
    case "downloading":  return "Downloading voice...";
    case "ocr":          return "Detecting text...";
    case "tts":          return "Generating speech...";
    case "playing":      return "Playing audio...";
    case "cancelled":    return "Cancelled";
    default:             return "";
  }
}


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
// This is the top-level component rendered inside the Decky sidebar panel.
// It switches between two modes:
//   - Normal mode: shows settings, credential status, and action buttons
//   - File browser mode: shows the FileBrowser for selecting a JSON file

function Content() {
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

  // --- Screen capture state ---
  // Status message shown after a capture attempt (success or error)
  const [captureMessage, setCaptureMessage] = useState<string | null>(null);
  // Whether the capture message is a success (green) or error (red)
  const [captureIsSuccess, setCaptureIsSuccess] = useState(false);
  // Whether a capture is currently in progress (disables the button)
  const [isCapturing, setIsCapturing] = useState(false);

  // --- OCR state ---
  // Status message shown after an OCR attempt (success or error)
  const [ocrMessage, setOcrMessage] = useState<string | null>(null);
  // Whether the OCR message is a success (green) or error (red)
  const [ocrIsSuccess, setOcrIsSuccess] = useState(false);
  // Whether OCR is currently running (disables the button)
  const [isOcrRunning, setIsOcrRunning] = useState(false);
  // The detected text from the last OCR run (shown in scrollable area)
  const [ocrText, setOcrText] = useState<string | null>(null);

  // --- TTS state ---
  // Status message shown after a TTS attempt (success or error)
  const [ttsMessage, setTtsMessage] = useState<string | null>(null);
  // Whether the TTS message is a success (green) or error (red)
  const [ttsIsSuccess, setTtsIsSuccess] = useState(false);
  // Whether TTS synthesis is currently running (disables the button)
  const [isTtsRunning, setIsTtsRunning] = useState(false);
  // Whether audio is currently playing (toggles Read Text / Stop button)
  const [isPlaying, setIsPlaying] = useState(false);
  // Ref for the playback polling interval (so we can clear it on unmount)
  const playbackPollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  // Ref for the volume save debounce timeout
  const volumeSaveTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

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
  // Status of the touchscreen monitor (fetched on mount and after changes)
  const [touchscreenStatus, setTouchscreenStatus] = useState<TouchscreenStatus | null>(null);
  // Ref for the touchscreen polling interval
  const touchPollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // --- Pipeline state (Phase 6: Read Screen) ---
  // Whether the end-to-end pipeline is currently running
  const [isPipelineRunning, setIsPipelineRunning] = useState(false);
  // Current pipeline step (for progress display)
  const [pipelineStep, setPipelineStep] = useState("idle");
  // Status message shown after pipeline completes (success or error)
  const [pipelineMessage, setPipelineMessage] = useState<string | null>(null);
  // Whether the pipeline message is a success (green) or error (red)
  const [pipelineIsSuccess, setPipelineIsSuccess] = useState(false);
  // Ref for the pipeline polling interval
  const pipelinePollRef = useRef<ReturnType<typeof setInterval> | null>(null);

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

  // Handle the "Test Capture" button press.
  // Calls the backend to capture a screenshot and shows the result.
  const handleTestCapture = async () => {
    setIsCapturing(true);
    setCaptureMessage(null);
    const result = await captureScreenshot();
    setIsCapturing(false);
    setCaptureMessage(result.message);
    setCaptureIsSuccess(result.success);
    // Auto-clear the status message after 5 seconds
    setTimeout(() => setCaptureMessage(null), 5000);
  };

  // Handle the "Test OCR" button press.
  // Captures a screenshot, runs it through Cloud Vision OCR, and displays the result.
  const handleTestOcr = async () => {
    setIsOcrRunning(true);
    setOcrMessage(null);
    setOcrText(null);  // Clear previous text

    const result = await performOcr();
    setIsOcrRunning(false);
    setOcrMessage(result.message);
    setOcrIsSuccess(result.success);

    // Show detected text if any was found
    if (result.text) {
      setOcrText(result.text);
    }

    // Auto-clear the status message after 8 seconds (longer than capture
    // because OCR results are more important to read)
    setTimeout(() => setOcrMessage(null), 8000);
  };

  // --- TTS handlers ---

  // Start polling the backend for playback status (every 1 second).
  // This detects when mpv finishes playing naturally so we can update the UI.
  const startPlaybackPoll = () => {
    // Clear any existing poll first
    stopPlaybackPoll();

    playbackPollRef.current = setInterval(async () => {
      const status = await getPlaybackStatus();
      if (!status.is_playing) {
        // Playback finished naturally — update UI
        setIsPlaying(false);
        stopPlaybackPoll();
      }
    }, 1000);
  };

  // Stop the playback polling interval
  const stopPlaybackPoll = () => {
    if (playbackPollRef.current) {
      clearInterval(playbackPollRef.current);
      playbackPollRef.current = null;
    }
  };

  // Handle the "Read Text" button — synthesize speech and start playback
  const handleReadText = async () => {
    if (!ocrText) return;

    setIsTtsRunning(true);
    setTtsMessage(null);

    const result = await performTts(ocrText);
    setIsTtsRunning(false);
    setTtsMessage(result.message);
    setTtsIsSuccess(result.success);

    if (result.success) {
      setIsPlaying(true);
      startPlaybackPoll();  // Poll to detect when playback finishes
    }

    // Auto-clear the status message after 5 seconds
    setTimeout(() => setTtsMessage(null), 5000);
  };

  // Handle the "Stop Playback" button — stop audio and update UI
  const handleStopPlayback = async () => {
    await stopPlayback();
    setIsPlaying(false);
    stopPlaybackPoll();
    setTtsMessage("Playback stopped");
    setTtsIsSuccess(true);
    setTimeout(() => setTtsMessage(null), 5000);
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

  // --- Pipeline handlers (Phase 6: Read Screen) ---

  // Start polling the backend for pipeline status (every 1 second).
  // Updates the progress step label in real time while the pipeline runs.
  const startPipelinePoll = () => {
    stopPipelinePoll();

    pipelinePollRef.current = setInterval(async () => {
      const status = await getPipelineStatus();
      setPipelineStep(status.step);

      // If the pipeline finished and audio is playing, switch to playback poll
      if (!status.running && status.is_playing) {
        setIsPipelineRunning(false);
        setIsPlaying(true);
        stopPipelinePoll();
        startPlaybackPoll();
      }
      // If the pipeline finished and nothing is playing, clean up
      if (!status.running && !status.is_playing) {
        setIsPipelineRunning(false);
        stopPipelinePoll();
      }
    }, 1000);
  };

  // Stop the pipeline polling interval
  const stopPipelinePoll = () => {
    if (pipelinePollRef.current) {
      clearInterval(pipelinePollRef.current);
      pipelinePollRef.current = null;
    }
  };

  // Handle the "Read Screen" button — run the full pipeline
  const handleReadScreen = async () => {
    setIsPipelineRunning(true);
    setPipelineStep("starting");
    setPipelineMessage(null);
    setOcrText(null);  // Clear previous OCR text
    startPipelinePoll();

    // This blocks until the pipeline finishes (capture → OCR → TTS → playback start)
    const result = await readScreen();

    // Populate OCR text if the pipeline got far enough to extract it
    if (result.text) {
      setOcrText(result.text);
    }

    if (result.success) {
      // Pipeline succeeded — audio is now playing
      setPipelineMessage(result.message);
      setPipelineIsSuccess(true);
      setIsPlaying(true);
      setIsPipelineRunning(false);
      stopPipelinePoll();
      startPlaybackPoll();
    } else {
      // Pipeline failed or was cancelled
      setPipelineMessage(result.message);
      setPipelineIsSuccess(false);
      setIsPipelineRunning(false);
      stopPipelinePoll();
    }

    // Auto-clear the status message after 8 seconds
    setTimeout(() => setPipelineMessage(null), 8000);
  };

  // Handle the "Stop" button during pipeline — cancel and clean up
  const handleStopPipeline = async () => {
    await stopPipeline();
    setIsPipelineRunning(false);
    setIsPlaying(false);
    setPipelineStep("idle");
    stopPipelinePoll();
    stopPlaybackPoll();
    setPipelineMessage("Pipeline stopped");
    setPipelineIsSuccess(true);
    setTimeout(() => setPipelineMessage(null), 5000);
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

  // --- Touchscreen polling helpers (Phase 9) ---

  // Start polling the touchscreen status (every 500ms when enabled)
  const startTouchPoll = () => {
    stopTouchPoll();
    touchPollRef.current = setInterval(async () => {
      const status = await getTouchscreenStatus();
      setTouchscreenStatus(status);
    }, 500);
  };

  // Stop the touchscreen polling interval
  const stopTouchPoll = () => {
    if (touchPollRef.current) {
      clearInterval(touchPollRef.current);
      touchPollRef.current = null;
    }
  };

  // Start/stop touchscreen polling based on the touchscreen_enabled setting
  // AND whether the monitor is actually connected. Only poll when both are
  // true to avoid spamming the backend with status calls when the device
  // isn't available.
  useEffect(() => {
    if (settings?.touchscreen_enabled && touchscreenStatus?.initialized) {
      startTouchPoll();
    } else {
      stopTouchPoll();
    }
    return () => stopTouchPoll();
  }, [settings?.touchscreen_enabled, touchscreenStatus?.initialized]);

  // Cleanup: clear intervals and timeouts when the component unmounts
  // to prevent memory leaks and stale state updates.
  useEffect(() => {
    return () => {
      stopPlaybackPoll();
      stopPipelinePoll();
      stopTouchPoll();
      if (volumeSaveTimeoutRef.current) {
        clearTimeout(volumeSaveTimeoutRef.current);
      }
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

  // --- Computed readiness flags (Phase 8: provider-aware) ---
  // Determines whether each provider has what it needs to function.
  // Local provider needs bundled Python; GCP needs credentials.
  const ocrReady = settings.ocr_provider === "local"
    ? settings.is_local_available
    : settings.is_gcp_configured;
  const ttsReady = settings.tts_provider === "local"
    ? settings.is_local_available
    : settings.is_gcp_configured;
  // Both providers must be ready for the full pipeline
  const pipelineReady = ocrReady && ttsReady;

  // Check if either provider uses GCP (controls GCP Credentials section visibility)
  const needsGcp = settings.ocr_provider === "gcp" || settings.tts_provider === "gcp";

  // --- Normal mode (settings view) ---
  return (
    <>
      {/* ---- Read Screen Section (Phase 6) ---- */}
      {/* This is the primary user-facing feature: one button to capture the
          screen, detect text, synthesize speech, and play it back. Placed first
          because it's the main action the user will use. */}
      <PanelSection title="Read Screen">
        {/* Progress indicator — shows the current pipeline step while running */}
        {isPipelineRunning && (
          <PanelSectionRow>
            <div style={{
              color: "#67b7dc",
              padding: "4px 0",
              fontSize: "13px"
            }}>
              {getPipelineStepLabel(pipelineStep)}
            </div>
          </PanelSectionRow>
        )}

        {/* Status message after pipeline completes (success/error) */}
        {pipelineMessage && !isPipelineRunning && (
          <PanelSectionRow>
            <div style={{
              color: pipelineIsSuccess ? "#2ecc71" : "#e74c3c",
              padding: "4px 0",
              fontSize: "13px"
            }}>
              {pipelineMessage}
            </div>
          </PanelSectionRow>
        )}

        {/* Read Screen / Stop button — toggles based on pipeline/playback state */}
        <PanelSectionRow>
          {isPipelineRunning || isPlaying ? (
            <ButtonItem
              layout="below"
              onClick={isPipelineRunning ? handleStopPipeline : handleStopPlayback}
            >
              <div style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: "8px" }}>
                <FaStop size={14} />
                <span>Stop</span>
              </div>
            </ButtonItem>
          ) : (
            <ButtonItem
              layout="below"
              onClick={handleReadScreen}
              disabled={!pipelineReady || !settings.enabled}
            >
              <div style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: "8px" }}>
                <FaBook size={14} />
                <span>Read Screen</span>
              </div>
            </ButtonItem>
          )}
        </PanelSectionRow>

        {/* Hint when providers aren't ready */}
        {!pipelineReady && (
          <PanelSectionRow>
            <div style={{
              color: "#b8bcbf",
              fontSize: "12px",
              padding: "4px 0"
            }}>
              {!ocrReady && settings.ocr_provider === "gcp"
                ? "Load GCP credentials to enable OCR"
                : !ocrReady
                ? "Local OCR unavailable (bundled Python missing)"
                : !ttsReady && settings.tts_provider === "gcp"
                ? "Load GCP credentials to enable TTS"
                : "Local TTS unavailable (bundled Python missing)"
              }
            </div>
          </PanelSectionRow>
        )}
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

        {/* Monitor status indicator — shows whether the device is connected */}
        {settings.trigger_button !== "disabled" && monitorStatus && (
          <PanelSectionRow>
            <Field label="Status">
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

        {/* Hint text explaining what the button trigger does */}
        <PanelSectionRow>
          <div style={{
            color: "#b8bcbf",
            fontSize: "12px",
            padding: "4px 0",
          }}>
            {settings.trigger_button === "disabled"
              ? "Enable a button to trigger Read Screen without opening this panel"
              : `Hold ${settings.trigger_button} for ${settings.hold_time_ms}ms to trigger Read Screen`
            }
          </div>
        </PanelSectionRow>
      </PanelSection>

      {/* ---- Touchscreen Section (Phase 9) ---- */}
      {/* Experimental touchscreen input — reads tap coordinates from the Steam
          Deck's capacitive touchscreen. Currently just displays coordinates for
          testing; future phases will add region selection and tap-to-read. */}
      <PanelSection title="Touchscreen">
        {/* Enable/disable toggle */}
        <PanelSectionRow>
          <ToggleField
            label="Touch Input"
            description="Read tap coordinates from touchscreen"
            checked={settings.touchscreen_enabled}
            onChange={async (value) => {
              await saveSetting("touchscreen_enabled", value);
              if (settings) {
                setSettings({ ...settings, touchscreen_enabled: value });
              }
              // Fetch updated status after toggling
              if (value) {
                // Small delay for the monitor to initialize
                setTimeout(async () => {
                  const status = await getTouchscreenStatus();
                  setTouchscreenStatus(status);
                }, 500);
              } else {
                setTouchscreenStatus(null);
              }
            }}
          />
        </PanelSectionRow>

        {/* Status indicator — shown when touchscreen is enabled */}
        {settings.touchscreen_enabled && touchscreenStatus && (
          <>
            <PanelSectionRow>
              <Field label="Status">
                <div style={{
                  color: touchscreenStatus.initialized ? "#2ecc71" : "#e74c3c",
                  fontWeight: "bold",
                  fontSize: "13px",
                }}>
                  {touchscreenStatus.initialized ? "Connected" : "Not connected"}
                </div>
              </Field>
            </PanelSectionRow>

            {/* Device path — shown when connected */}
            {touchscreenStatus.initialized && touchscreenStatus.device_path && (
              <PanelSectionRow>
                <Field label="Device">
                  <div style={{ color: "#b8bcbf", fontSize: "12px" }}>
                    {touchscreenStatus.device_path}
                  </div>
                </Field>
              </PanelSectionRow>
            )}

            {/* Last touch coordinates — shown when connected */}
            {touchscreenStatus.initialized && (
              <PanelSectionRow>
                <Field label="Last Touch">
                  <div style={{ color: "#67b7dc", fontSize: "13px" }}>
                    {touchscreenStatus.last_touch
                      ? `(${touchscreenStatus.last_touch.x}, ${touchscreenStatus.last_touch.y})`
                      : "Tap the screen to test"
                    }
                  </div>
                </Field>
              </PanelSectionRow>
            )}

            {/* Physical dimensions info — shown when connected */}
            {touchscreenStatus.initialized && touchscreenStatus.physical_max_x > 0 && (
              <PanelSectionRow>
                <div style={{
                  color: "#b8bcbf",
                  fontSize: "11px",
                  padding: "4px 0",
                }}>
                  Physical: {touchscreenStatus.physical_max_x} x {touchscreenStatus.physical_max_y} → Logical: 1280 x 800
                </div>
              </PanelSectionRow>
            )}
          </>
        )}

        {/* Hint text */}
        <PanelSectionRow>
          <div style={{
            color: "#b8bcbf",
            fontSize: "12px",
            padding: "4px 0",
          }}>
            {settings.touchscreen_enabled
              ? "Tap coordinates will appear above. Used for future region selection."
              : "Enable to read touchscreen tap positions (experimental)"
            }
          </div>
        </PanelSectionRow>
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

      {/* ---- Settings Section ---- */}
      <PanelSection title="Settings">
        {/* Master on/off toggle */}
        <PanelSectionRow>
          <ToggleField
            label="Enabled"
            description="Master switch — disables triggers and OCR/TTS"
            checked={settings.enabled}
            onChange={(value) => handleToggle("enabled", value)}
          />
        </PanelSectionRow>

        {/* Debug mode toggle */}
        <PanelSectionRow>
          <ToggleField
            label="Debug Mode"
            description="Show extra diagnostic logging"
            checked={settings.debug}
            onChange={(value) => handleToggle("debug", value)}
          />
        </PanelSectionRow>
      </PanelSection>

      {/* ---- Screen Capture Section ---- */}
      <PanelSection title="Screen Capture">
        {/* Status message from the last capture attempt */}
        {captureMessage && (
          <PanelSectionRow>
            <div style={{
              color: captureIsSuccess ? "#2ecc71" : "#e74c3c",
              padding: "4px 0",
              fontSize: "13px"
            }}>
              {captureMessage}
            </div>
          </PanelSectionRow>
        )}

        {/* Test Capture button — triggers a screenshot and shows the result */}
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={handleTestCapture}
            disabled={isCapturing || isPipelineRunning}
          >
            {isCapturing ? "Capturing..." : "Test Capture"}
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>

      {/* ---- OCR Section ---- */}
      <PanelSection title="OCR (Text Detection)">
        {/* Status message from the last OCR attempt */}
        {ocrMessage && (
          <PanelSectionRow>
            <div style={{
              color: ocrIsSuccess ? "#2ecc71" : "#e74c3c",
              padding: "4px 0",
              fontSize: "13px"
            }}>
              {ocrMessage}
            </div>
          </PanelSectionRow>
        )}

        {/* Test OCR button — captures screenshot + runs OCR via selected provider */}
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={handleTestOcr}
            disabled={isOcrRunning || !ocrReady || isPipelineRunning || !settings.enabled}
          >
            {isOcrRunning ? "Running OCR..." : "Test OCR"}
          </ButtonItem>
        </PanelSectionRow>

        {/* Hint when OCR provider isn't ready */}
        {!ocrReady && (
          <PanelSectionRow>
            <div style={{
              color: "#b8bcbf",
              fontSize: "12px",
              padding: "4px 0"
            }}>
              {settings.ocr_provider === "gcp"
                ? "Load GCP credentials to enable OCR"
                : "Local OCR unavailable (bundled Python missing)"
              }
            </div>
          </PanelSectionRow>
        )}

        {/* Scrollable text display — shows detected text from the last OCR run */}
        {ocrText && (
          <PanelSectionRow>
            <div style={{
              maxHeight: "200px",
              overflow: "auto",
              backgroundColor: "#1a1a2e",
              borderRadius: "4px",
              padding: "8px",
              fontSize: "12px",
              lineHeight: "1.4",
              whiteSpace: "pre-wrap",    // Preserve line breaks from OCR
              wordBreak: "break-word",   // Break long words to prevent overflow
              color: "#e0e0e0",
              width: "100%",
            }}>
              {ocrText}
            </div>
          </PanelSectionRow>
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
                  ?? VOICE_OPTIONS[1].data
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

        {/* TTS status message (success/error) */}
        {ttsMessage && (
          <PanelSectionRow>
            <div style={{
              color: ttsIsSuccess ? "#2ecc71" : "#e74c3c",
              padding: "4px 0",
              fontSize: "13px"
            }}>
              {ttsMessage}
            </div>
          </PanelSectionRow>
        )}

        {/* Read Text / Stop Playback button — toggles based on playback state */}
        <PanelSectionRow>
          {isPlaying ? (
            <ButtonItem
              layout="below"
              onClick={handleStopPlayback}
            >
              <div style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: "8px" }}>
                <FaStop size={14} />
                <span>Stop Playback</span>
              </div>
            </ButtonItem>
          ) : (
            <ButtonItem
              layout="below"
              onClick={handleReadText}
              disabled={isTtsRunning || !ocrText || !ttsReady || isPipelineRunning || !settings.enabled}
            >
              <div style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: "8px" }}>
                <FaVolumeUp size={14} />
                <span>{isTtsRunning ? "Synthesizing..." : "Read Text"}</span>
              </div>
            </ButtonItem>
          )}
        </PanelSectionRow>

        {/* Hint when no OCR text is available */}
        {!ocrText && ttsReady && (
          <PanelSectionRow>
            <div style={{
              color: "#b8bcbf",
              fontSize: "12px",
              padding: "4px 0"
            }}>
              Run OCR above first to get text for reading
            </div>
          </PanelSectionRow>
        )}

        {/* Hint when TTS provider isn't ready */}
        {!ttsReady && (
          <PanelSectionRow>
            <div style={{
              color: "#b8bcbf",
              fontSize: "12px",
              padding: "4px 0"
            }}>
              {settings.tts_provider === "gcp"
                ? "Load GCP credentials to enable TTS"
                : "Local TTS unavailable (bundled Python missing)"
              }
            </div>
          </PanelSectionRow>
        )}
      </PanelSection>
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

  return {
    // The name shown in the Decky plugin list
    name: "Cloud Reader",

    // The styled title at the top of the plugin's sidebar panel
    titleView: (
      <div className={staticClasses.Title}>Cloud Reader</div>
    ),

    // The main content of the plugin panel
    content: <Content />,

    // The icon shown in the plugin list sidebar (book icon)
    icon: <FaBook />,

    // Called when the plugin is unloaded (e.g., Decky Loader restarts)
    onDismount() {
      console.log("Decky Cloud Reader: frontend plugin unloaded");
    },
  };
});
