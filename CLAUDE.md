# Decky Cloud Reader Plugin

## Project Overview

This is a **Decky Loader plugin** for Steam Deck. It is a classic Decky plugin that runs within the Decky Loader framework.

**Purpose:** Use GCP services (Cloud Vision OCR + Cloud Text-to-Speech) to provide OCR and TTS functionality for text-heavy games on Steam Deck.

## Development Environment

- **Host machine:** M1 MacBook Pro (ARM / Apple Silicon)
- **Target device:** Steam Deck at IP `192.168.50.116` with SSH configured and passwordless sudo
- **Build/test architecture:** All local testing and builds must be done inside an **x86 Docker image** with **Python 3.13** (matching the Steam Deck's Python version) before deploying to the Deck

## Development Workflow

- Work in **small incremental steps**, testing each change immediately on the target Steam Deck
- Build and validate locally in the x86 Docker container first
- Deploy to Steam Deck via SSH for on-device testing
- **Comment code extensively** and provide detailed explanations in output — treat me as someone unfamiliar with the structures, approaches, frameworks, and programming languages being used, so I can learn as we go
- **Challenge vague requests** — if I ask for something in vague terms, don't just execute it blindly. Question my decision, propose better alternatives if they exist, and ask clarifying questions to gather enough context before proceeding

## Reference Projects (Local Clones)

### Decky Plugin Template (primary reference)
- **Path:** `/Users/mshabalov/Documents/claude-projects/decky-plugin-template`
- Use as the main structural reference for plugin scaffolding, build system, and conventions

### Decky-Translator (UI and input reference)
- **Path:** `/Users/mshabalov/Documents/claude-projects/Decky-Translator`
- Reference for:
  - Navigation panel implementation in a Decky plugin
  - Using the **L4 button** on Steam Deck to trigger plugin actions without opening the plugin UI

### decky-ocr-tts-claude-service-plugin (feature reference)
- **Path:** `/Users/mshabalov/Documents/claude-projects/decky-ocr-tts-claude-service-plugin`
- Contains a **working GCP + OCR + TTS plugin** implementation
- **Architecture note:** This plugin uses a separate Python service, which is NOT the desired architecture for our new plugin
- **Useful for:** Borrowing UI features and Python OCR/TTS logic to adapt into our integrated implementation
