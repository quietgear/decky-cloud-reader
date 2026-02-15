// =============================================================================
// Decky Cloud Reader — Frontend Entry Point
// =============================================================================
//
// This file defines the plugin's UI that appears in the Decky Loader sidebar.
// It uses React components from @decky/ui (Steam's design system) and the
// @decky/api bridge to call Python backend methods.
//
// How frontend↔backend communication works:
// - `callable<[args], returnType>("python_method_name")` creates a typed
//   function that, when called, sends an RPC to the Python backend (main.py)
//   and returns the result as a Promise.
// - The Python method must be an async method on the Plugin class.
// =============================================================================

import {
  ButtonItem,       // A clickable button styled for the Steam UI
  PanelSection,     // A collapsible section in the plugin sidebar panel
  PanelSectionRow,  // A row within a PanelSection
  staticClasses     // CSS class names for standard Steam UI styling
} from "@decky/ui";

import {
  callable,         // Creates a typed function that calls a Python backend method
  definePlugin      // Registers this module as a Decky plugin
} from "@decky/api";

import { useState } from "react";

// FaBook icon — fits the "reader" theme of this plugin.
// react-icons/fa provides Font Awesome icons as React components.
import { FaBook } from "react-icons/fa";

// -----------------------------------------------------------------------------
// Backend RPC bindings
// -----------------------------------------------------------------------------

// `get_greeting` is a Python method on the Plugin class in main.py.
// callable<[], string> means: takes no arguments, returns a string.
// When called, it sends an RPC to the backend and resolves with the result.
const getGreeting = callable<[], string>("get_greeting");

// -----------------------------------------------------------------------------
// Content component — the main plugin panel UI
// -----------------------------------------------------------------------------

function Content() {
  // State to hold the greeting message returned from the Python backend.
  // Starts as null (no response yet), then gets set after the button is clicked.
  const [greeting, setGreeting] = useState<string | null>(null);

  // Handler for the "Test Backend Connection" button.
  // Calls the Python backend's get_greeting() method and stores the result.
  const onTestBackend = async () => {
    const result = await getGreeting();
    setGreeting(result);
  };

  return (
    // PanelSection creates a titled, collapsible section in the sidebar
    <PanelSection title="Cloud Reader">
      <PanelSectionRow>
        <ButtonItem
          layout="below"
          onClick={onTestBackend}
        >
          {/* Show the greeting if we have one, otherwise show the button label */}
          {greeting ?? "Test Backend Connection"}
        </ButtonItem>
      </PanelSectionRow>
    </PanelSection>
  );
}

// -----------------------------------------------------------------------------
// Plugin registration
// -----------------------------------------------------------------------------
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
