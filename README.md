# Decky Cloud Reader

An OCR and Text-to-Speech accessibility plugin for Steam Deck. Captures the game screen, recognizes text, and reads it aloud — designed for text-heavy games like RPGs, visual novels, and strategy games.

## Features

- **Two provider modes** — Local (offline, works out of the box) and Google Cloud (online, higher accuracy)
- **5 capture modes** — Full screen, swipe selection, two-tap selection, fixed region, and hybrid
- **Hardware button triggers** — Use the back grip buttons (L4, R4, L5, R5) with configurable hold time
- **Touchscreen triggers** — Tap or swipe to select text regions
- **16+ local voices** in 14 languages — downloaded on demand (~63 MB each)
- **28 GCP voices** across 9 languages — Neural2, WaveNet, and Standard
- **Text filtering** — Remove unwanted words from OCR results before reading
- **Sound effects** — Audio feedback for capture start, end, and stop events
- **Overlay preview** — Visual region preview for fixed region capture mode

## Requirements

- Steam Deck with SteamOS 3.x
- [Decky Loader](https://github.com/SteamDeckHomebrew/decky-loader) installed
- (Optional) Google Cloud account for GCP mode — see [Google Cloud Setup Guide](GOOGLE_CLOUD_SETUP.md)

## Installation

### From GitHub Releases

1. Download `decky-cloud-reader.zip` from the [Releases page](../../releases/latest)
2. Transfer the zip file to your Steam Deck (USB drive, SCP, KDE Connect, or browser download)
3. Switch to **Gaming Mode**
4. Press the **...** (Quick Access) button > **Decky** tab (plug icon)
5. Open Decky **Settings** (gear icon) > **Developer**
6. Enable **Developer Mode** if not already enabled
7. Click **Install Plugin From ZIP**
8. Navigate to and select the downloaded zip file

The plugin is ready to use immediately — **local mode** (offline OCR + TTS) is the default and requires no additional setup.

## Quick Start

1. Install the plugin (see above)
2. Open the Quick Access Menu (**...** button) > **Decky** tab
3. Find **Decky Cloud Reader** and make sure **Enabled** is toggled on
4. Launch a game with on-screen text
5. Hold the **L4** back grip button for 0.5 seconds (default) to capture and read the screen

Tap the screen during playback to stop it.

## Provider Modes

### Local (Default)

Works completely offline with no setup required.

- **OCR:** RapidOCR with PaddleOCR v4 ONNX models
- **TTS:** Piper TTS with on-demand voice downloads from HuggingFace
- **Performance:** ~2s per read (warm worker), ~6s first read (cold start)

### GCP (Online)

Higher accuracy OCR and more natural-sounding voices. Requires a Google Cloud account with Vision and Text-to-Speech APIs enabled.

- **OCR:** Google Cloud Vision API
- **TTS:** Google Cloud Text-to-Speech API (Neural2, WaveNet, Standard voices)
- **Setup:** See [Google Cloud Setup Guide](GOOGLE_CLOUD_SETUP.md)
- **Cost:** Free tier covers typical personal use (1,000 OCR requests/month, 1M TTS characters/month)

You can mix providers — for example, use GCP for OCR and local for TTS, or vice versa.

## Capture Modes

| Mode | Description |
|------|-------------|
| **Full Screen** | Captures the entire game screen |
| **Swipe Selection** | Draw a rectangle on screen by swiping to select a region |
| **Two-Tap Selection** | Tap two points to define opposite corners of a region |
| **Fixed Region** | Captures a pre-configured screen area (set via sliders in settings) |
| **Hybrid** | Button trigger reads the fixed region; touchscreen tap starts two-tap selection |

## Trigger Methods

### Hardware Buttons

Use the Steam Deck's back grip buttons to trigger the OCR/TTS pipeline:

| Setting | Options |
|---------|---------|
| **Button** | L4 (default), R4, L5, R5, or Disabled |
| **Hold Time** | Instant (0ms), 200ms, 500ms (default), 800ms, 1000ms, 1500ms |

### Touchscreen

Available in capture modes that use touch input (swipe selection, two-tap selection, hybrid). The touchscreen is automatically enabled/disabled based on the selected capture mode.

## Configuration

All settings are accessible from the plugin panel in Decky's Quick Access Menu.

| Setting | Default | Description |
|---------|---------|-------------|
| **Enabled** | On | Master switch — disables all processing when off |
| **OCR Provider** | Local | `Local` or `Google Cloud` |
| **TTS Provider** | Local | `Local` or `Google Cloud` |
| **Local Voice** | en_US-amy-medium | Piper TTS voice (auto-downloads on first use) |
| **Local Speech Rate** | Medium | Slow, Medium, or Fast |
| **GCP Voice** | en-US-Neural2-C | Google Cloud TTS voice |
| **GCP Speech Rate** | Medium | x-slow, slow, medium, fast, x-fast |
| **Volume** | 100 | TTS volume (0–100) |
| **Trigger Button** | L4 | Back grip button or Disabled |
| **Hold Time** | 500ms | How long to hold the button before triggering |
| **Capture Mode** | Full Screen | Screen capture method |
| **Mute Sounds** | Off | Disable UI feedback sounds |

## Google Cloud Setup

GCP mode is **optional** — local mode works without any cloud setup.

If you want to use GCP for higher accuracy OCR or more natural voices, follow the [Google Cloud Setup Guide](GOOGLE_CLOUD_SETUP.md). The guide walks you through:

1. Creating a Google Cloud account (free tier available)
2. Enabling the Vision and Text-to-Speech APIs
3. Creating a service account with the right permissions
4. Downloading and loading the credentials JSON file into the plugin

## Building from Source

The build runs entirely inside Docker — it cross-compiles to x86_64 Linux regardless of your host platform.

### Prerequisites

- [Docker](https://www.docker.com/products/docker-desktop/) (Docker Desktop on Mac/Windows, or Docker Engine on Linux)
- Git

### Build

```bash
git clone https://github.com/mshabalov/decky-cloud-reader.git
cd decky-cloud-reader
mkdir -p dist
docker compose -f docker/docker-compose.yml build
docker compose -f docker/docker-compose.yml up
```

The output zip will be at `dist/decky-cloud-reader.zip`.

> **Note:** The first build downloads OCR models (~100 MB), a bundled Python 3.12 interpreter (~40 MB), and Python dependencies for both providers. Subsequent builds use Docker layer caching and are much faster. Use `docker compose -f docker/docker-compose.yml build --no-cache` for a fully clean rebuild.

### Deploy to Steam Deck (Development)

For development, use the deploy script to build, upload, and restart the plugin in one step:

```bash
# Edit deploy.sh to set your Steam Deck's IP address
./deploy.sh          # Build + deploy (default)
./deploy.sh build    # Build only
./deploy.sh deploy   # Deploy only (uses existing zip)
```

Requires SSH key access to `deck@<your-deck-ip>` with passwordless sudo.

### Releasing a New Version

1. Update the version in `package.json`
2. Commit: `git commit -am "Bump version to X.Y.Z"`
3. Tag: `git tag vX.Y.Z`
4. Push: `git push origin main --tags`
5. GitHub Actions will automatically build the plugin and create a release on the [Releases page](../../releases)

## Architecture

```
Frontend (TypeScript/React)           Backend (Python)
┌──────────────────────────┐         ┌─────────────────────────────────┐
│ Decky QAM Panel          │   RPC   │ main.py (Plugin class)          │
│  - Enabled toggle        │◄───────►│  - Pipeline orchestration       │
│  - Provider selection    │         │  - Screen capture (GStreamer)    │
│  - Voice / rate / volume │         │  - Dual worker lifecycle mgmt   │
│  - Button trigger config │         │  - Audio playback               │
│  - Capture mode config   │         │                                 │
│  - Text filter config    │         │  Workers (persistent subprocs): │
│                          │         │  - gcp_worker.py (Python 3.13)  │
│ Region Preview Overlay   │         │  - local_worker.py (Python 3.12)│
│  - Fixed region preview  │         │                                 │
└──────────────────────────┘         │  Input monitors (threads):      │
                                     │  - hidraw_monitor.py (buttons)  │
                                     │  - touchscreen_monitor.py       │
                                     └─────────────────────────────────┘
```

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Plugin doesn't appear in Decky | Restart Decky Loader: in Desktop Mode, run `sudo systemctl restart plugin_loader` |
| No audio output | Check volume setting in plugin. Verify audio works in SteamOS normally |
| OCR returns empty text | Make sure the screen has readable text. Try full screen capture mode |
| Local TTS voice not working | The voice downloads on first use (~63 MB). Check your internet connection |
| GCP errors | Verify APIs are enabled and credentials are loaded (see [GCP Setup Guide](GOOGLE_CLOUD_SETUP.md)) |
| Button trigger not working | Check that a button is selected (not "Disabled") and Enabled is on |
| Touchscreen not responding | Only active in capture modes that use touch (swipe, two-tap, hybrid). Disabled during on-screen keyboard |

### Viewing Logs

On the Steam Deck (Desktop Mode, Konsole):

```bash
journalctl -u plugin_loader -f | grep DCR
```

Enable **Debug Mode** in the plugin settings for more detailed logging.

## License

[MIT](LICENSE)
