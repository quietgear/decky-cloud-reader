#!/usr/bin/env bash
# =============================================================================
# Decky Cloud Reader — Build & Deploy Script
# =============================================================================
#
# This script handles the full build-and-deploy pipeline:
#   1. BUILD:  Compile the plugin inside an x86 Docker container and produce
#              a zip file ready for Decky Loader's manual install.
#   2. DEPLOY: Upload the zip to the Steam Deck, remove any existing version
#              of the plugin, extract the new version, and restart Decky Loader.
#
# Usage:
#   ./deploy.sh          # Build + deploy (default)
#   ./deploy.sh build    # Build only (produces dist/decky-cloud-reader.zip)
#   ./deploy.sh deploy   # Deploy only (uses existing zip)
#
# Prerequisites:
#   - Docker Desktop running (with x86 emulation for Apple Silicon)
#   - SSH key configured for deck@192.168.50.58 (passwordless login)
#   - Passwordless sudo on the Steam Deck (for restarting Decky Loader)
#
# The zip file produced by "build" is compatible with Decky Loader's
# developer mode "Install Plugin From ZIP" feature, so you can also
# install it manually through the Decky UI without this script.
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Steam Deck connection info
DECK_USER="deck"
DECK_HOST="192.168.50.58"
DECK_SSH="${DECK_USER}@${DECK_HOST}"

# Plugin folder name — must match the directory inside the zip file.
# Decky Loader discovers plugins by scanning /home/deck/homebrew/plugins/
PLUGIN_NAME="decky-cloud-reader"
DECK_PLUGINS_DIR="/home/deck/homebrew/plugins"
DECK_PLUGIN_DIR="${DECK_PLUGINS_DIR}/${PLUGIN_NAME}"

# Local path to the zip file produced by the Docker build
ZIP_FILE="dist/${PLUGIN_NAME}.zip"

# ---------------------------------------------------------------------------
# Build function
# ---------------------------------------------------------------------------
# Runs Docker Compose to:
#   1. Build the frontend (TypeScript → JavaScript) in an x86 container
#   2. Install Python dependencies into py_modules/ (if requirements.txt exists)
#   3. Assemble the plugin directory structure and zip it
#
# Output: dist/decky-cloud-reader.zip
build() {
    echo "=== Building plugin in Docker ==="

    # Clean previous build artifacts to avoid stale files
    rm -rf dist/

    # Ensure the dist/ directory exists for the Docker volume mount
    mkdir -p dist/

    # --build: rebuild the Docker image before starting containers.
    # Docker layer caching is used for speed — unchanged layers (system deps,
    # Node modules, Python packages, model downloads) are reused automatically.
    # Use `docker compose build --no-cache` manually if you need a fully clean build
    # (e.g., after changing requirements.txt or model URLs).
    docker compose -f docker/docker-compose.yml build
    docker compose -f docker/docker-compose.yml up

    # Verify the zip was produced
    if [ ! -f "${ZIP_FILE}" ]; then
        echo "ERROR: Build failed — ${ZIP_FILE} not found"
        exit 1
    fi

    echo ""
    echo "=== Build successful ==="
    echo "Output: ${ZIP_FILE}"
    # Show zip size in a human-friendly way
    ls -lh "${ZIP_FILE}"
}

# ---------------------------------------------------------------------------
# Deploy function
# ---------------------------------------------------------------------------
# Uploads the zip to the Steam Deck, performs a clean replacement of the
# plugin directory, and restarts Decky Loader to pick up changes.
#
# Steps:
#   1. SCP the zip file to a temp location on the Deck
#   2. Remove the old plugin directory (clean slate — no stale files)
#   3. Unzip the new version into the plugins directory
#   4. Clean up the temp zip file
#   5. Restart the Decky Loader systemd service
deploy() {
    echo "=== Deploying to Steam Deck (${DECK_HOST}) ==="

    # Verify the zip file exists before trying to deploy
    if [ ! -f "${ZIP_FILE}" ]; then
        echo "ERROR: No zip file found at ${ZIP_FILE}"
        echo "Run './deploy.sh build' first."
        exit 1
    fi

    # Upload the zip to a temp location on the Steam Deck.
    # We use /tmp/ so it doesn't matter if the plugin dir doesn't exist yet.
    echo "--- Uploading zip to Steam Deck ---"
    scp "${ZIP_FILE}" "${DECK_SSH}:/tmp/${PLUGIN_NAME}.zip"

    # SSH into the Deck and perform the install:
    #   1. Remove the old plugin directory completely (clean install)
    #   2. Unzip the new version into the plugins directory
    #   3. Clean up the temp zip file
    echo "--- Installing plugin on Steam Deck ---"
    ssh "${DECK_SSH}" "
        sudo rm -rf ${DECK_PLUGIN_DIR} &&
        sudo rm -f /home/deck/homebrew/settings/decky-cloud-reader/settings.json &&
        sudo unzip -o /tmp/${PLUGIN_NAME}.zip -d ${DECK_PLUGINS_DIR}/ &&
        rm /tmp/${PLUGIN_NAME}.zip &&
        echo 'Plugin installed to ${DECK_PLUGIN_DIR}'
    "

    # Restart the Decky Loader systemd service so it discovers our plugin.
    echo "--- Restarting Decky Loader ---"
    ssh "${DECK_SSH}" "sudo systemctl restart plugin_loader"

    echo ""
    echo "=== Deploy complete ==="
    echo "Plugin installed to ${DECK_PLUGIN_DIR}"
    echo "Open Quick Access (... button) → Decky tab to verify"
}

# ---------------------------------------------------------------------------
# Main — parse command and run
# ---------------------------------------------------------------------------
case "${1:-all}" in
    build)
        build
        ;;
    deploy)
        deploy
        ;;
    all)
        build
        deploy
        ;;
    *)
        echo "Usage: ./deploy.sh [build|deploy|all]"
        echo "  build  — Build plugin zip in Docker"
        echo "  deploy — Deploy existing zip to Steam Deck"
        echo "  all    — Build and deploy (default)"
        exit 1
        ;;
esac
