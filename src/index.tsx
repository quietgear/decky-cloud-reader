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
  staticClasses     // CSS class names for standard Steam UI styling
} from "@decky/ui";

import {
  callable,         // Creates a typed function that calls a Python backend method
  definePlugin      // Registers this module as a Decky plugin
} from "@decky/api";

import { useState, useEffect } from "react";

// FaBook icon — fits the "reader" theme of this plugin.
// FaFolder/FaFile icons — used in the file browser for visual clarity.
import { FaBook, FaFolder, FaFileAlt, FaArrowLeft } from "react-icons/fa";


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

// Current plugin settings returned by get_settings() backend RPC
interface PluginSettings {
  voice_id: string;        // TTS voice (Phase 5)
  speech_rate: string;     // TTS speed preset (Phase 5)
  volume: number;          // TTS volume 0-100 (Phase 5)
  enabled: boolean;        // Master on/off
  debug: boolean;          // Verbose logging
  is_configured: boolean;  // Computed: are GCP credentials loaded?
  project_id: string;      // Computed: GCP project ID from credentials
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

  // Load settings from the backend when the component first mounts.
  // Also reload when returning from file browser mode.
  useEffect(() => {
    const loadSettings = async () => {
      const result = await getSettings();
      setSettings(result);
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

  // --- Normal mode (settings view) ---
  return (
    <>
      {/* ---- GCP Credentials Section ---- */}
      <PanelSection title="GCP Credentials">
        {/* Status: Configured or Not Configured */}
        <PanelSectionRow>
          <Field label="Status">
            <div style={{
              color: settings.is_configured ? "#2ecc71" : "#e74c3c",
              fontWeight: "bold"
            }}>
              {settings.is_configured ? "Configured" : "Not Configured"}
            </div>
          </Field>
        </PanelSectionRow>

        {/* Show project ID when credentials are loaded */}
        {settings.is_configured && settings.project_id && (
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
            {settings.is_configured ? "Change Credentials" : "Load Credentials"}
          </ButtonItem>
        </PanelSectionRow>

        {/* Clear Credentials button — only shown when credentials are loaded */}
        {settings.is_configured && (
          <PanelSectionRow>
            <ButtonItem layout="below" onClick={handleClearCredentials}>
              Clear Credentials
            </ButtonItem>
          </PanelSectionRow>
        )}
      </PanelSection>

      {/* ---- Settings Section ---- */}
      <PanelSection title="Settings">
        {/* Master on/off toggle */}
        <PanelSectionRow>
          <ToggleField
            label="Enabled"
            description="Master switch for the plugin"
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
            disabled={isCapturing}
          >
            {isCapturing ? "Capturing..." : "Test Capture"}
          </ButtonItem>
        </PanelSectionRow>
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
