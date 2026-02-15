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

import decky


class Plugin:

    # -------------------------------------------------------------------------
    # Lifecycle: _main()
    # -------------------------------------------------------------------------
    # Called once when Decky Loader first loads this plugin.
    # This is where you'd set up any long-running tasks, open connections,
    # or initialize resources.
    async def _main(self):
        decky.logger.info("Decky Cloud Reader: backend loaded")

    # -------------------------------------------------------------------------
    # Lifecycle: _unload()
    # -------------------------------------------------------------------------
    # Called when the plugin is stopped (e.g., Decky Loader restarts, or the
    # user disables the plugin). The plugin is NOT removed from disk — just
    # deactivated. Clean up any running tasks or open connections here.
    async def _unload(self):
        decky.logger.info("Decky Cloud Reader: backend unloaded")

    # -------------------------------------------------------------------------
    # Lifecycle: _uninstall()
    # -------------------------------------------------------------------------
    # Called after _unload() when the plugin is fully removed (deleted from
    # disk). Use this to clean up any persistent data, config files, or
    # downloaded models that the plugin created.
    async def _uninstall(self):
        decky.logger.info("Decky Cloud Reader: backend uninstalled")

    # -------------------------------------------------------------------------
    # RPC method: get_greeting()
    # -------------------------------------------------------------------------
    # Called from the frontend via:
    #   const getGreeting = callable<[], string>("get_greeting");
    #   const result = await getGreeting();
    #
    # This is a simple test method to verify that frontend↔backend
    # communication is working. Returns a greeting string.
    async def get_greeting(self):
        decky.logger.info("get_greeting() called from frontend")
        return "Hello from the Python backend!"
