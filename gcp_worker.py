#!/usr/bin/env python3
# =============================================================================
# Decky Cloud Reader — GCP Worker Subprocess
# =============================================================================
#
# This script runs under the SYSTEM Python (/usr/bin/python3), NOT Decky's
# embedded Python. It exists as a separate process because:
#   - Decky's embedded Python can't load google-cloud native libraries (C extensions)
#   - System Python can, with PYTHONPATH pointing to py_modules/
#
# Communication contract:
#   - Input:  CLI args for action + file paths; credentials via env var
#   - Output: Single JSON object to stdout (success/error result)
#   - Logs:   All diagnostic messages go to stderr (picked up by parent process)
#
# Usage:
#   GCP_CREDENTIALS_BASE64=... python3 gcp_worker.py ocr /tmp/screenshot.png
#
# IMPORTANT: This file must NOT import `decky` — it doesn't exist in system Python.
# =============================================================================

import sys
import os
import json
import base64
import time
import traceback
from io import BytesIO

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Prefix for all log messages so they can be identified in combined stderr output.
# Different from the main plugin's "[DCR]" to distinguish subprocess logs.
WORKER_LOG = "[DCR-worker]"

# Google Cloud Vision API has a 20MB limit, but we use 10MB as a safe threshold
# to avoid slow uploads and potential timeouts on Steam Deck's WiFi.
MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10 MB

# Retry settings for transient API errors (503 Service Unavailable,
# 429 Too Many Requests, timeouts). We retry up to 3 times with increasing
# delays to give the API time to recover.
MAX_RETRIES = 3
RETRY_DELAYS = [0.5, 1.0]  # Delays between attempts (seconds)

# Google Cloud TTS API has a 5000-byte input limit per request.
# We truncate text beyond this to avoid API errors.
MAX_TEXT_LENGTH = 5000

# Voice registry: maps voice IDs to their language codes.
# These are all Neural2 voices — Google's high-quality neural voices.
# 4 US English + 4 British English voices for Phase 5. Expandable later.
VOICE_REGISTRY = {
    # US English Neural2 voices (en-US)
    "en-US-Neural2-A": "en-US",  # Male
    "en-US-Neural2-C": "en-US",  # Female
    "en-US-Neural2-D": "en-US",  # Male
    "en-US-Neural2-F": "en-US",  # Female
    # British English Neural2 voices (en-GB)
    "en-GB-Neural2-A": "en-GB",  # Female
    "en-GB-Neural2-B": "en-GB",  # Male
    "en-GB-Neural2-C": "en-GB",  # Female
    "en-GB-Neural2-D": "en-GB",  # Male
}

# Speech rate presets: human-friendly names mapped to the float value
# that the Cloud TTS API expects in AudioConfig.speaking_rate.
# Range is 0.25 to 4.0, where 1.0 is normal speed.
SPEECH_RATE_MAP = {
    "x-slow": 0.5,
    "slow": 0.75,
    "medium": 1.0,
    "fast": 1.25,
    "x-fast": 1.5,
}


# ---------------------------------------------------------------------------
# Logging helpers — all output goes to stderr, never stdout
# ---------------------------------------------------------------------------
# stdout is reserved for the JSON result. Any print() to stdout would corrupt
# the JSON parsing in the parent process.

def log_info(msg):
    """Log an informational message to stderr."""
    print(f"{WORKER_LOG} {msg}", file=sys.stderr, flush=True)


def log_error(msg):
    """Log an error message to stderr."""
    print(f"{WORKER_LOG} ERROR: {msg}", file=sys.stderr, flush=True)


def log_debug(msg):
    """Log a debug message to stderr."""
    print(f"{WORKER_LOG} DEBUG: {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Output helpers — JSON to stdout
# ---------------------------------------------------------------------------
# The parent process (main.py) reads stdout and parses it as JSON.
# We must output exactly ONE JSON object, then exit.

def output_result(data):
    """
    Write a success result as JSON to stdout and exit cleanly.

    Args:
        data: Dictionary to serialize as JSON. Should contain at minimum
              a "success" key.
    """
    print(json.dumps(data), flush=True)
    sys.exit(0)


def output_error(message):
    """
    Write an error result as JSON to stdout and exit with code 1.

    Args:
        message: Human-readable error description.
    """
    print(json.dumps({"success": False, "message": message}), flush=True)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Vision client initialization
# ---------------------------------------------------------------------------

def init_vision_client(creds_b64):
    """
    Create a Google Cloud Vision client from base64-encoded service account JSON.

    The credentials are passed via environment variable (not CLI args) to avoid
    exposing them in `ps` output on the system.

    Args:
        creds_b64: Base64-encoded string containing the full service account JSON.

    Returns:
        An initialized ImageAnnotatorClient ready to make API calls.

    Raises:
        Exception: If credentials are invalid or client creation fails.
    """
    # Step 1: Decode the base64 string back to JSON
    creds_json = json.loads(base64.b64decode(creds_b64))

    # Step 2: Create Google OAuth2 credentials from the service account info.
    # This is the standard way to authenticate with GCP APIs using a service account.
    from google.oauth2 import service_account
    credentials = service_account.Credentials.from_service_account_info(creds_json)

    # Step 3: Create the Vision API client with these credentials.
    # The client handles HTTP transport, request serialization, etc.
    from google.cloud import vision
    client = vision.ImageAnnotatorClient(credentials=credentials)

    log_info("Vision client initialized")
    return client


# ---------------------------------------------------------------------------
# TTS client initialization
# ---------------------------------------------------------------------------

def init_tts_client(creds_b64):
    """
    Create a Google Cloud Text-to-Speech client from base64-encoded service
    account JSON. Same pattern as init_vision_client().

    Args:
        creds_b64: Base64-encoded string containing the full service account JSON.

    Returns:
        An initialized TextToSpeechClient ready to make API calls.

    Raises:
        Exception: If credentials are invalid or client creation fails.
    """
    # Step 1: Decode the base64 string back to JSON
    creds_json = json.loads(base64.b64decode(creds_b64))

    # Step 2: Create Google OAuth2 credentials from the service account info
    from google.oauth2 import service_account
    credentials = service_account.Credentials.from_service_account_info(creds_json)

    # Step 3: Create the TTS API client with these credentials
    from google.cloud import texttospeech
    client = texttospeech.TextToSpeechClient(credentials=credentials)

    log_info("TTS client initialized")
    return client


# ---------------------------------------------------------------------------
# Image resizing (two-stage: quality reduction, then dimension scaling)
# ---------------------------------------------------------------------------

def resize_image_if_needed(image_bytes):
    """
    Resize an image if it exceeds MAX_IMAGE_SIZE (10 MB).

    Uses a two-stage approach borrowed from the reference plugin:
      1. Reduce JPEG quality from 85 down to 20 (fast, preserves dimensions)
      2. If still too large, scale dimensions down from 80% to 30% (slower)

    This ensures the image fits within the Vision API's size limits while
    preserving as much quality as possible.

    Args:
        image_bytes: Raw PNG/JPEG bytes of the screenshot.

    Returns:
        The original bytes if already small enough, or resized JPEG bytes.
    """
    if len(image_bytes) <= MAX_IMAGE_SIZE:
        return image_bytes

    log_info(f"Image too large ({len(image_bytes):,} bytes), resizing...")

    from PIL import Image

    # Open the image from bytes
    img = Image.open(BytesIO(image_bytes))

    # Convert RGBA/palette images to RGB (required for JPEG format)
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")

    # Leave a 100 KB margin below the limit to account for any overhead
    target_size = MAX_IMAGE_SIZE - 100_000

    # --- Stage 1: Reduce JPEG quality ---
    # Start at quality=85 and decrease by 10 each iteration.
    # This is the fastest way to reduce file size.
    quality = 85
    output = BytesIO()

    while quality > 20:
        output.seek(0)
        output.truncate()
        img.save(output, format="JPEG", quality=quality)

        if output.tell() <= target_size:
            log_info(f"Resized via quality reduction (q={quality}): {output.tell():,} bytes")
            return output.getvalue()

        quality -= 10

    # --- Stage 2: Scale dimensions down ---
    # If quality reduction alone wasn't enough, shrink the image dimensions.
    # Start at 80% and go down to 30%.
    scale = 0.8
    while scale > 0.3:
        new_size = (int(img.width * scale), int(img.height * scale))
        resized = img.resize(new_size, Image.Resampling.LANCZOS)

        output.seek(0)
        output.truncate()
        resized.save(output, format="JPEG", quality=quality)

        if output.tell() <= target_size:
            log_info(f"Resized via scaling ({scale:.0%}): {output.tell():,} bytes")
            return output.getvalue()

        scale -= 0.1

    # Return whatever we got — it's the smallest we can make it
    log_info(f"Resized to minimum: {output.tell():,} bytes")
    return output.getvalue()


# ---------------------------------------------------------------------------
# OCR action — the main text detection pipeline
# ---------------------------------------------------------------------------

def do_ocr(image_path, creds_b64):
    """
    Perform OCR on an image file using Google Cloud Vision API.

    Steps:
      1. Read the image file from disk
      2. Resize if over 10 MB
      3. Initialize the Vision client with credentials
      4. Call text_detection() with retry on transient errors
      5. Parse the response and extract detected text

    Args:
        image_path: Absolute path to the screenshot PNG file.
        creds_b64: Base64-encoded GCP service account JSON.

    Returns:
        Never returns — calls output_result() or output_error() which exit.
    """
    # Step 1: Read the image file
    if not os.path.exists(image_path):
        output_error(f"Image file not found: {image_path}")

    with open(image_path, "rb") as f:
        image_bytes = f.read()

    file_size = len(image_bytes)
    log_info(f"Read image: {file_size:,} bytes from {image_path}")

    if file_size == 0:
        output_error("Image file is empty (0 bytes)")

    # Step 2: Resize if needed (Vision API has a size limit)
    image_bytes = resize_image_if_needed(image_bytes)

    # Step 3: Initialize the Vision client
    try:
        client = init_vision_client(creds_b64)
    except Exception as e:
        log_error(f"Failed to init Vision client: {e}")
        output_error(f"Failed to initialize GCP credentials: {e}")

    # Step 4: Call the Vision API with retry logic
    from google.cloud import vision
    from google.api_core import exceptions as google_exceptions

    image = vision.Image(content=image_bytes)
    log_info(f"Sending {len(image_bytes):,} bytes to Vision API...")

    # Retry loop: attempts the API call up to MAX_RETRIES times.
    # On transient errors (503, 429, timeouts), we wait and retry.
    # On permanent errors, we fail immediately.
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            response = client.text_detection(image=image)
            break  # Success — exit the retry loop
        except (
            google_exceptions.ServiceUnavailable,    # 503
            google_exceptions.ResourceExhausted,     # 429 (rate limit)
            google_exceptions.DeadlineExceeded,      # 504 (timeout)
            ConnectionError,
            ConnectionResetError,
        ) as e:
            last_error = e
            if attempt < MAX_RETRIES - 1:
                # Calculate delay: use the delays list, clamping to last value
                delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                log_info(
                    f"Transient error (attempt {attempt + 1}/{MAX_RETRIES}): {e}. "
                    f"Retrying in {delay}s..."
                )
                time.sleep(delay)
            else:
                log_error(f"Failed after {MAX_RETRIES} attempts: {e}")
                output_error(f"Vision API failed after {MAX_RETRIES} retries: {e}")
        except Exception as e:
            # Non-transient error — fail immediately, don't retry
            log_error(f"Vision API error: {e}")
            log_error(traceback.format_exc())
            output_error(f"Vision API error: {e}")
    else:
        # This runs if the for loop completed without break (all retries failed)
        output_error(f"Vision API failed after {MAX_RETRIES} retries: {last_error}")

    # Step 5: Check for API-level errors in the response
    if response.error.message:
        log_error(f"Vision API response error: {response.error.message}")
        output_error(f"Vision API error: {response.error.message}")

    # Step 6: Extract the detected text
    if not response.text_annotations:
        log_info("No text detected in image")
        output_result({
            "success": True,
            "text": "",
            "char_count": 0,
            "line_count": 0,
            "message": "No text detected in image",
        })

    # The first annotation contains the full concatenated text from all
    # detected text blocks. Subsequent annotations are individual words/blocks.
    detected_text = response.text_annotations[0].description
    char_count = len(detected_text)
    # Count lines: split by newline, empty text = 0 lines
    line_count = detected_text.count("\n") + 1 if detected_text else 0

    log_info(f"Detected {char_count:,} chars, {line_count} lines")
    log_debug(f"Text preview: {detected_text[:200]}...")

    output_result({
        "success": True,
        "text": detected_text,
        "char_count": char_count,
        "line_count": line_count,
        "message": f"OCR complete: {char_count:,} chars, {line_count} lines",
    })


# ---------------------------------------------------------------------------
# TTS action — synthesize speech from text
# ---------------------------------------------------------------------------

def do_tts(text, output_path, voice_id, speech_rate, creds_b64):
    """
    Synthesize speech from text using Google Cloud Text-to-Speech API.

    Steps:
      1. Validate and truncate text if needed
      2. Look up voice language code and speaking rate
      3. Initialize the TTS client with credentials
      4. Build synthesis request (input, voice, audio config)
      5. Call synthesize_speech() with retry on transient errors
      6. Write audio content to output file

    Args:
        text: The text to synthesize into speech.
        output_path: Absolute path where the MP3 file will be written.
        voice_id: Voice name from VOICE_REGISTRY (e.g., "en-US-Neural2-C").
        speech_rate: Speed preset from SPEECH_RATE_MAP (e.g., "medium").
        creds_b64: Base64-encoded GCP service account JSON.

    Returns:
        Never returns — calls output_result() or output_error() which exit.
    """
    # Step 1: Validate text input
    if not text or not text.strip():
        output_error("No text provided for TTS")

    # Truncate if over the API limit (5000 bytes). Append a note so the user
    # knows the audio doesn't cover the full text.
    original_length = len(text)
    if len(text) > MAX_TEXT_LENGTH:
        text = text[:MAX_TEXT_LENGTH - 30] + "\n... (text truncated)"
        log_info(f"Text truncated from {original_length:,} to {len(text):,} chars")

    log_info(f"TTS input: {len(text):,} chars, voice={voice_id}, rate={speech_rate}")

    # Step 2: Look up language code from the voice registry.
    # Fall back to "en-US" if the voice ID isn't recognized (shouldn't happen
    # with normal usage, but be defensive).
    language_code = VOICE_REGISTRY.get(voice_id, "en-US")
    if voice_id not in VOICE_REGISTRY:
        log_info(f"Unknown voice '{voice_id}', defaulting to language_code='en-US'")

    # Look up speaking rate float from the presets map.
    # Fall back to 1.0 (normal speed) if the preset isn't recognized.
    speaking_rate = SPEECH_RATE_MAP.get(speech_rate, 1.0)
    if speech_rate not in SPEECH_RATE_MAP:
        log_info(f"Unknown speech rate '{speech_rate}', defaulting to 1.0")

    # Step 3: Initialize the TTS client
    try:
        client = init_tts_client(creds_b64)
    except Exception as e:
        log_error(f"Failed to init TTS client: {e}")
        output_error(f"Failed to initialize GCP credentials: {e}")

    # Step 4: Build the synthesis request components
    from google.cloud import texttospeech

    # SynthesisInput — the text to convert to speech.
    # We use plain text (not SSML) for simplicity.
    synthesis_input = texttospeech.SynthesisInput(text=text)

    # VoiceSelectionParams — which voice to use.
    # language_code must match the voice name's prefix.
    voice_params = texttospeech.VoiceSelectionParams(
        language_code=language_code,
        name=voice_id,
    )

    # AudioConfig — output format and speaking rate.
    # MP3 is compact and supported by mpv on Steam Deck.
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.MP3,
        speaking_rate=speaking_rate,
    )

    # Step 5: Call the TTS API with retry logic (same pattern as OCR)
    from google.api_core import exceptions as google_exceptions

    log_info("Sending TTS request to Cloud TTS API...")

    last_error = None
    response = None
    for attempt in range(MAX_RETRIES):
        try:
            response = client.synthesize_speech(
                input=synthesis_input,
                voice=voice_params,
                audio_config=audio_config,
            )
            break  # Success — exit the retry loop
        except (
            google_exceptions.ServiceUnavailable,    # 503
            google_exceptions.ResourceExhausted,     # 429 (rate limit)
            google_exceptions.DeadlineExceeded,      # 504 (timeout)
            ConnectionError,
            ConnectionResetError,
        ) as e:
            last_error = e
            if attempt < MAX_RETRIES - 1:
                delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                log_info(
                    f"Transient error (attempt {attempt + 1}/{MAX_RETRIES}): {e}. "
                    f"Retrying in {delay}s..."
                )
                time.sleep(delay)
            else:
                log_error(f"TTS failed after {MAX_RETRIES} attempts: {e}")
                output_error(f"TTS API failed after {MAX_RETRIES} retries: {e}")
        except Exception as e:
            # Non-transient error — fail immediately, don't retry
            log_error(f"TTS API error: {e}")
            log_error(traceback.format_exc())
            output_error(f"TTS API error: {e}")
    else:
        # All retries exhausted without break
        output_error(f"TTS API failed after {MAX_RETRIES} retries: {last_error}")

    # Step 6: Write the audio content to the output file
    audio_size = len(response.audio_content)
    log_info(f"TTS response: {audio_size:,} bytes of audio")

    try:
        with open(output_path, "wb") as f:
            f.write(response.audio_content)
        log_info(f"Audio written to {output_path}")
    except Exception as e:
        log_error(f"Failed to write audio file: {e}")
        output_error(f"Failed to write audio file: {e}")

    output_result({
        "success": True,
        "audio_size": audio_size,
        "output_path": output_path,
        "text_length": len(text),
        "voice_id": voice_id,
        "message": f"TTS complete: {audio_size:,} bytes, voice={voice_id}",
    })


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    """
    Parse CLI arguments and dispatch to the appropriate action handler.

    Usage: python3 gcp_worker.py <action> [args...]

    Actions:
      ocr <image_path>                              — Perform OCR on the given image file
      tts <text> <output_path> <voice_id> <rate>     — Synthesize speech from text

    Credentials are read from the GCP_CREDENTIALS_BASE64 environment variable.
    """
    # Validate we got at least an action argument
    if len(sys.argv) < 2:
        output_error("Usage: gcp_worker.py <action> [args...]")

    action = sys.argv[1]

    # Read credentials from environment (not CLI args — avoids ps exposure)
    creds_b64 = os.environ.get("GCP_CREDENTIALS_BASE64", "")
    if not creds_b64:
        output_error("GCP_CREDENTIALS_BASE64 environment variable not set")

    # Dispatch to the appropriate action handler
    if action == "ocr":
        # OCR requires an image path argument
        if len(sys.argv) < 3:
            output_error("Usage: gcp_worker.py ocr <image_path>")
        image_path = sys.argv[2]
        do_ocr(image_path, creds_b64)

    elif action == "tts":
        # TTS requires: text, output_path, voice_id, speech_rate
        if len(sys.argv) < 6:
            output_error("Usage: gcp_worker.py tts <text> <output_path> <voice_id> <speech_rate>")
        text = sys.argv[2]
        output_path = sys.argv[3]
        voice_id = sys.argv[4]
        speech_rate = sys.argv[5]
        do_tts(text, output_path, voice_id, speech_rate, creds_b64)

    else:
        output_error(f"Unknown action: {action}")


if __name__ == "__main__":
    main()
