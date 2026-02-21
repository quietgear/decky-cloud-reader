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
# Two operating modes:
#   1. CLI mode (one-shot): run a single action, output JSON, exit
#      GCP_CREDENTIALS_BASE64=... python3 gcp_worker.py ocr /tmp/screenshot.png
#   2. Serve mode (persistent): stay alive, read JSON commands from stdin,
#      write JSON responses to stdout, reuse pre-initialized GCP clients
#      GCP_CREDENTIALS_BASE64=... python3 gcp_worker.py serve
#
# Communication contract:
#   - CLI mode: CLI args for action + file paths; single JSON to stdout; exit
#   - Serve mode: JSON lines on stdin/stdout; first line is {"ready": true}
#   - Logs: All diagnostic messages go to stderr (picked up by parent process)
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
# 28 voices across 9 languages (Phase 20). Includes Neural2, Wavenet, and
# Standard voices for: EN-US, EN-GB, UK, DE, FR, ES, JA, PT-BR, RU.
VOICE_REGISTRY = {
    # US English Neural2 voices (en-US)
    "en-US-Neural2-A": "en-US",  # Male
    "en-US-Neural2-C": "en-US",  # Female
    "en-US-Neural2-D": "en-US",  # Male
    "en-US-Neural2-F": "en-US",  # Female
    # US English Wavenet voices (en-US)
    "en-US-Wavenet-C": "en-US",  # Female
    "en-US-Wavenet-D": "en-US",  # Male
    # British English Neural2 voices (en-GB)
    "en-GB-Neural2-A": "en-GB",  # Female
    "en-GB-Neural2-B": "en-GB",  # Male
    "en-GB-Neural2-C": "en-GB",  # Female
    "en-GB-Neural2-D": "en-GB",  # Male
    # Ukrainian voices (uk-UA)
    "uk-UA-Wavenet-A": "uk-UA",   # Female (Wavenet)
    "uk-UA-Standard-A": "uk-UA",  # Female (Standard)
    # German Neural2 voices (de-DE)
    "de-DE-Neural2-A": "de-DE",  # Female
    "de-DE-Neural2-B": "de-DE",  # Male
    "de-DE-Neural2-C": "de-DE",  # Female
    "de-DE-Neural2-D": "de-DE",  # Male
    # French Neural2 voices (fr-FR)
    "fr-FR-Neural2-A": "fr-FR",  # Female
    "fr-FR-Neural2-B": "fr-FR",  # Male
    "fr-FR-Neural2-C": "fr-FR",  # Female
    "fr-FR-Neural2-D": "fr-FR",  # Male
    # Spanish Neural2 voices (es-ES)
    "es-ES-Neural2-A": "es-ES",  # Female
    "es-ES-Neural2-B": "es-ES",  # Male
    # Japanese Neural2 voices (ja-JP)
    "ja-JP-Neural2-B": "ja-JP",  # Female
    "ja-JP-Neural2-C": "ja-JP",  # Male
    "ja-JP-Neural2-D": "ja-JP",  # Male
    # Portuguese (Brazil) Neural2 voices (pt-BR)
    "pt-BR-Neural2-A": "pt-BR",  # Female
    "pt-BR-Neural2-B": "pt-BR",  # Male
    "pt-BR-Neural2-C": "pt-BR",  # Female
    # Russian voices (ru-RU)
    "ru-RU-Wavenet-A": "ru-RU",   # Female (Wavenet)
    "ru-RU-Wavenet-B": "ru-RU",   # Male (Wavenet)
    "ru-RU-Standard-A": "ru-RU",  # Female (Standard)
    "ru-RU-Standard-B": "ru-RU",  # Male (Standard)
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

# Phase 25: OCR language → Vision API language hints mapping.
# Maps our language IDs to BCP-47 codes that the Vision API uses as hints
# for text detection. Hints improve accuracy for non-Latin scripts.
# See: https://cloud.google.com/vision/docs/languages
OCR_LANGUAGE_HINTS = {
    "english": ["en"],
    "chinese": ["zh", "ja"],         # Chinese + Japanese share CJK characters
    "korean": ["ko"],
    "latin": ["fr", "de", "es", "it", "pt"],  # Major Latin-script languages
    "eslav": ["ru", "uk", "bg", "be"],         # Cyrillic-script languages
    "thai": ["th"],
    "greek": ["el"],
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
# Exception-based flow control
# ---------------------------------------------------------------------------
# The do_* action functions use these exceptions to deliver results back to
# the dispatcher (CLI main() or persistent serve() loop). This replaces the
# old sys.exit() pattern, allowing the same do_* functions to work in both
# one-shot CLI mode and long-running serve mode.
#
# Flow: do_ocr() → output_result(data) → raises WorkerResult → caught by
#       main() or serve(), which writes JSON to stdout.

class WorkerResult(Exception):
    """Raised to deliver a successful result from a do_* function."""
    def __init__(self, data):
        self.data = data

class WorkerError(Exception):
    """Raised to deliver an error result from a do_* function."""
    def __init__(self, message):
        self.data = {"success": False, "message": message}


def output_result(data):
    """
    Deliver a success result by raising WorkerResult.

    In CLI mode (main()), the exception propagates up to the dispatcher
    which prints JSON to stdout and calls sys.exit(0).
    In serve mode (serve()), the exception is caught and the JSON is
    written to stdout, then the loop continues to the next command.

    Args:
        data: Dictionary to serialize as JSON. Should contain at minimum
              a "success" key.
    """
    raise WorkerResult(data)


def output_error(message):
    """
    Deliver an error result by raising WorkerError.

    Same propagation pattern as output_result() — the dispatcher catches
    the exception and handles JSON output + exit/continue.

    Args:
        message: Human-readable error description.
    """
    raise WorkerError(message)


# ---------------------------------------------------------------------------
# Credential helpers — shared by all client initializations
# ---------------------------------------------------------------------------
# When running the combined ocr_tts action, we decode the base64 credentials
# ONCE and pass the resulting dict to both Vision and TTS client init. This
# avoids paying the JSON parse + base64 decode cost twice.

def _decode_credentials(creds_b64):
    """
    Decode base64-encoded GCP service account credentials to a Python dict.

    Args:
        creds_b64: Base64-encoded string containing the full service account JSON.

    Returns:
        Dict with the service account JSON fields (project_id, private_key, etc.)
    """
    return json.loads(base64.b64decode(creds_b64))


def _make_oauth_credentials(creds_json):
    """
    Create Google OAuth2 credentials from a decoded service account dict.

    Args:
        creds_json: Dict from _decode_credentials() — the parsed service account JSON.

    Returns:
        A google.oauth2.service_account.Credentials object.
    """
    from google.oauth2 import service_account
    return service_account.Credentials.from_service_account_info(creds_json)


# ---------------------------------------------------------------------------
# Vision client initialization
# ---------------------------------------------------------------------------

def init_vision_client(creds_b64, creds_json=None):
    """
    Create a Google Cloud Vision client from GCP service account credentials.

    Supports two modes:
      1. Pass creds_b64 (base64 string) — decodes internally (used by standalone OCR)
      2. Pass creds_json (pre-decoded dict) — skips decoding (used by combined ocr_tts)

    Args:
        creds_b64: Base64-encoded string containing the full service account JSON.
                   Ignored if creds_json is provided.
        creds_json: Optional pre-decoded credentials dict. If provided, creds_b64
                    is not decoded again (saves time in combined actions).

    Returns:
        An initialized ImageAnnotatorClient ready to make API calls.

    Raises:
        Exception: If credentials are invalid or client creation fails.
    """
    # Decode credentials if not already provided as a dict
    if creds_json is None:
        creds_json = _decode_credentials(creds_b64)

    # Create OAuth2 credentials and the Vision API client
    credentials = _make_oauth_credentials(creds_json)

    from google.cloud import vision
    client = vision.ImageAnnotatorClient(credentials=credentials)

    log_info("Vision client initialized")
    return client


# ---------------------------------------------------------------------------
# TTS client initialization
# ---------------------------------------------------------------------------

def init_tts_client(creds_b64, creds_json=None):
    """
    Create a Google Cloud Text-to-Speech client from GCP service account credentials.

    Supports two modes (same as init_vision_client):
      1. Pass creds_b64 — decodes internally (used by standalone TTS)
      2. Pass creds_json — skips decoding (used by combined ocr_tts)

    Args:
        creds_b64: Base64-encoded string containing the full service account JSON.
                   Ignored if creds_json is provided.
        creds_json: Optional pre-decoded credentials dict.

    Returns:
        An initialized TextToSpeechClient ready to make API calls.

    Raises:
        Exception: If credentials are invalid or client creation fails.
    """
    # Decode credentials if not already provided as a dict
    if creds_json is None:
        creds_json = _decode_credentials(creds_b64)

    # Create OAuth2 credentials and the TTS API client
    credentials = _make_oauth_credentials(creds_json)

    from google.cloud import texttospeech
    client = texttospeech.TextToSpeechClient(credentials=credentials)

    log_info("TTS client initialized")
    return client


# ---------------------------------------------------------------------------
# Image cropping helper (Phase 12 capture modes)
# ---------------------------------------------------------------------------

def _crop_image_bytes(image_bytes, crop_region):
    """
    Crop raw image bytes to the specified bounding box.

    Opens image bytes via PIL, crops, and re-encodes to PNG bytes.
    Coordinates are clamped to image bounds and normalized (min/max swap).
    Returns original bytes if the crop region is too small (< 10px).

    Args:
        image_bytes: Raw PNG/JPEG bytes of the image.
        crop_region: Dict with keys "x1", "y1", "x2", "y2" (pixel coordinates).

    Returns:
        Cropped image as PNG bytes, or original bytes if region is too small.
    """
    from PIL import Image

    img = Image.open(BytesIO(image_bytes))
    w, h = img.size

    x1 = max(0, min(int(crop_region.get("x1", 0)), w))
    y1 = max(0, min(int(crop_region.get("y1", 0)), h))
    x2 = max(0, min(int(crop_region.get("x2", w)), w))
    y2 = max(0, min(int(crop_region.get("y2", h)), h))

    # Normalize: ensure x1 < x2 and y1 < y2
    if x1 > x2:
        x1, x2 = x2, x1
    if y1 > y2:
        y1, y2 = y2, y1

    # Reject tiny regions (< 10px) — likely accidental
    if (x2 - x1) < 10 or (y2 - y1) < 10:
        log_info(f"Crop region too small ({x2 - x1}x{y2 - y1}), using full image")
        return image_bytes

    log_info(f"Cropping image to ({x1},{y1})-({x2},{y2}) = {x2 - x1}x{y2 - y1}")
    cropped = img.crop((x1, y1, x2, y2))

    # Convert back to PNG bytes
    output = BytesIO()
    cropped.save(output, format="PNG")
    return output.getvalue()


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

def do_ocr(image_path, creds_b64, vision_client=None, crop_region=None, ocr_language=None):
    """
    Perform OCR on an image file using Google Cloud Vision API.

    Steps:
      1. Read the image file from disk
      2. Crop to region if specified (Phase 12)
      3. Resize if over 10 MB
      4. Initialize the Vision client with credentials (skip if pre-initialized)
      5. Call text_detection() with retry on transient errors (+ language hints)
      6. Parse the response and extract detected text

    Args:
        image_path: Absolute path to the screenshot PNG file.
        creds_b64: Base64-encoded GCP service account JSON.
        vision_client: Optional pre-initialized Vision client. When provided
                       (in serve mode), skips client creation for speed.
        crop_region: Optional dict {"x1", "y1", "x2", "y2"} defining the
                    bounding box to crop before OCR. If None, full image is used.
        ocr_language: OCR language identifier (e.g., "english", "chinese").
                     Used to provide language hints to the Vision API for
                     improved accuracy on non-English text.

    Returns:
        Never returns — raises WorkerResult or WorkerError via output_result/output_error.
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

    # Step 1b: Crop to region if specified (Phase 12 capture modes)
    if crop_region:
        image_bytes = _crop_image_bytes(image_bytes, crop_region)

    # Step 2: Resize if needed (Vision API has a size limit)
    image_bytes = resize_image_if_needed(image_bytes)

    # Step 3: Initialize the Vision client (skip if pre-initialized in serve mode)
    if vision_client is not None:
        client = vision_client
    else:
        try:
            client = init_vision_client(creds_b64)
        except Exception as e:
            log_error(f"Failed to init Vision client: {e}")
            output_error(f"Failed to initialize GCP credentials: {e}")

    # Step 4: Call the Vision API with retry logic
    from google.cloud import vision
    from google.api_core import exceptions as google_exceptions

    image = vision.Image(content=image_bytes)

    # Phase 25: Build language hints from ocr_language setting.
    # Language hints improve accuracy for non-Latin scripts (CJK, Cyrillic, etc.)
    image_context = None
    if ocr_language and ocr_language in OCR_LANGUAGE_HINTS:
        hints = OCR_LANGUAGE_HINTS[ocr_language]
        image_context = vision.ImageContext(language_hints=hints)
        log_info(f"Sending {len(image_bytes):,} bytes to Vision API (language_hints={hints})...")
    else:
        log_info(f"Sending {len(image_bytes):,} bytes to Vision API...")

    # Retry loop: attempts the API call up to MAX_RETRIES times.
    # On transient errors (503, 429, timeouts), we wait and retry.
    # On permanent errors, we fail immediately.
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            response = client.text_detection(image=image, image_context=image_context)
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

def do_tts(text, output_path, voice_id, speech_rate, creds_b64, tts_client=None):
    """
    Synthesize speech from text using Google Cloud Text-to-Speech API.

    Steps:
      1. Validate and truncate text if needed
      2. Look up voice language code and speaking rate
      3. Initialize the TTS client with credentials (skip if pre-initialized)
      4. Build synthesis request (input, voice, audio config)
      5. Call synthesize_speech() with retry on transient errors
      6. Write audio content to output file

    Args:
        text: The text to synthesize into speech.
        output_path: Absolute path where the MP3 file will be written.
        voice_id: Voice name from VOICE_REGISTRY (e.g., "en-US-Neural2-C").
        speech_rate: Speed preset from SPEECH_RATE_MAP (e.g., "medium").
        creds_b64: Base64-encoded GCP service account JSON.
        tts_client: Optional pre-initialized TTS client. When provided
                    (in serve mode), skips client creation for speed.

    Returns:
        Never returns — raises WorkerResult or WorkerError via output_result/output_error.
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

    # Step 3: Initialize the TTS client (skip if pre-initialized in serve mode)
    if tts_client is not None:
        client = tts_client
    else:
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
# Combined OCR+TTS action — single subprocess for the pipeline
# ---------------------------------------------------------------------------

def do_ocr_tts(image_path, output_mp3_path, voice_id, speech_rate, creds_b64,
               vision_client=None, tts_client=None, crop_region=None, ocr_language=None):
    """
    Perform OCR and TTS in a single invocation.

    In CLI mode, this is the speed-optimized path: one subprocess for both
    OCR and TTS, sharing Python startup, imports, and credential decode.

    In serve mode, pre-initialized clients are passed in, so credential
    decode and client init are skipped entirely (already done at startup).

    Steps:
      1. Decode credentials once (skipped if both clients provided)
      2. Read, crop if specified (Phase 12), and resize image
      3. Init Vision client → call text_detection() with retry (+ language hints)
      4. If no text → return early (success with empty text, no audio)
      5. Init TTS client (reuses decoded credentials) → synthesize_speech()
      6. Write MP3 to output path

    Args:
        image_path: Absolute path to the screenshot PNG file.
        output_mp3_path: Absolute path where the MP3 file will be written.
        voice_id: Voice name from VOICE_REGISTRY (e.g., "en-US-Neural2-C").
        speech_rate: Speed preset from SPEECH_RATE_MAP (e.g., "medium").
        creds_b64: Base64-encoded GCP service account JSON.
        vision_client: Optional pre-initialized Vision client (serve mode).
        tts_client: Optional pre-initialized TTS client (serve mode).
        crop_region: Optional dict {"x1", "y1", "x2", "y2"} defining the
                    bounding box to crop before OCR. If None, full image is used.
        ocr_language: OCR language identifier (e.g., "english", "chinese").
                     Used to provide language hints to the Vision API.

    Returns:
        Never returns — raises WorkerResult or WorkerError via output_result/output_error.
    """
    # ---- Step 1: Decode credentials ONCE for both clients ----
    # In serve mode, both clients are pre-initialized, so skip credential
    # decode entirely. If only one is missing, decode and init that one.
    creds_json = None
    if vision_client is None or tts_client is None:
        try:
            creds_json = _decode_credentials(creds_b64)
        except Exception as e:
            log_error(f"Failed to decode credentials: {e}")
            output_error(f"Failed to decode GCP credentials: {e}")

    # ---- Step 2: Read and resize the image ----
    if not os.path.exists(image_path):
        output_error(f"Image file not found: {image_path}")

    with open(image_path, "rb") as f:
        image_bytes = f.read()

    file_size = len(image_bytes)
    log_info(f"Read image: {file_size:,} bytes from {image_path}")

    if file_size == 0:
        output_error("Image file is empty (0 bytes)")

    # Crop to region if specified (Phase 12 capture modes)
    if crop_region:
        image_bytes = _crop_image_bytes(image_bytes, crop_region)

    image_bytes = resize_image_if_needed(image_bytes)

    # ---- Step 3: OCR — init Vision client and call text_detection() ----
    if vision_client is None:
        try:
            vision_client = init_vision_client(creds_b64, creds_json=creds_json)
        except Exception as e:
            log_error(f"Failed to init Vision client: {e}")
            output_error(f"Failed to initialize Vision credentials: {e}")

    from google.cloud import vision
    from google.api_core import exceptions as google_exceptions

    image = vision.Image(content=image_bytes)

    # Phase 25: Build language hints from ocr_language setting
    image_context = None
    if ocr_language and ocr_language in OCR_LANGUAGE_HINTS:
        hints = OCR_LANGUAGE_HINTS[ocr_language]
        image_context = vision.ImageContext(language_hints=hints)
        log_info(f"Sending {len(image_bytes):,} bytes to Vision API (language_hints={hints})...")
    else:
        log_info(f"Sending {len(image_bytes):,} bytes to Vision API...")

    # Retry loop for OCR (same pattern as do_ocr)
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            response = vision_client.text_detection(image=image, image_context=image_context)
            break
        except (
            google_exceptions.ServiceUnavailable,
            google_exceptions.ResourceExhausted,
            google_exceptions.DeadlineExceeded,
            ConnectionError,
            ConnectionResetError,
        ) as e:
            last_error = e
            if attempt < MAX_RETRIES - 1:
                delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                log_info(
                    f"OCR transient error (attempt {attempt + 1}/{MAX_RETRIES}): {e}. "
                    f"Retrying in {delay}s..."
                )
                time.sleep(delay)
            else:
                log_error(f"OCR failed after {MAX_RETRIES} attempts: {e}")
                output_error(f"Vision API failed after {MAX_RETRIES} retries: {e}")
        except Exception as e:
            log_error(f"Vision API error: {e}")
            log_error(traceback.format_exc())
            output_error(f"Vision API error: {e}")
    else:
        output_error(f"Vision API failed after {MAX_RETRIES} retries: {last_error}")

    # Check for API-level errors
    if response.error.message:
        log_error(f"Vision API response error: {response.error.message}")
        output_error(f"Vision API error: {response.error.message}")

    # ---- Step 4: Extract text — return early if none detected ----
    if not response.text_annotations:
        log_info("No text detected in image")
        output_result({
            "success": True,
            "text": "",
            "char_count": 0,
            "line_count": 0,
            "audio_size": 0,
            "output_path": "",
            "voice_id": voice_id,
            "message": "No text detected in image",
        })

    detected_text = response.text_annotations[0].description
    char_count = len(detected_text)
    line_count = detected_text.count("\n") + 1 if detected_text else 0
    log_info(f"OCR detected {char_count:,} chars, {line_count} lines")

    # ---- Step 5: TTS — init TTS client (reuses creds_json) and synthesize ----
    # Truncate text if over the API limit
    tts_text = detected_text
    if len(tts_text) > MAX_TEXT_LENGTH:
        tts_text = tts_text[:MAX_TEXT_LENGTH - 30] + "\n... (text truncated)"
        log_info(f"Text truncated from {len(detected_text):,} to {len(tts_text):,} chars")

    # Look up voice language code and speaking rate
    language_code = VOICE_REGISTRY.get(voice_id, "en-US")
    if voice_id not in VOICE_REGISTRY:
        log_info(f"Unknown voice '{voice_id}', defaulting to language_code='en-US'")

    speaking_rate = SPEECH_RATE_MAP.get(speech_rate, 1.0)
    if speech_rate not in SPEECH_RATE_MAP:
        log_info(f"Unknown speech rate '{speech_rate}', defaulting to 1.0")

    if tts_client is None:
        try:
            tts_client = init_tts_client(creds_b64, creds_json=creds_json)
        except Exception as e:
            log_error(f"Failed to init TTS client: {e}")
            # OCR succeeded but TTS init failed — return OCR text so it's not lost
            output_error(f"TTS credential init failed (OCR text available): {e}")

    from google.cloud import texttospeech

    synthesis_input = texttospeech.SynthesisInput(text=tts_text)
    voice_params = texttospeech.VoiceSelectionParams(
        language_code=language_code,
        name=voice_id,
    )
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.MP3,
        speaking_rate=speaking_rate,
    )

    log_info(f"Sending TTS request: {len(tts_text):,} chars, voice={voice_id}, rate={speech_rate}")

    # Retry loop for TTS (same pattern as do_tts)
    last_error = None
    tts_response = None
    for attempt in range(MAX_RETRIES):
        try:
            tts_response = tts_client.synthesize_speech(
                input=synthesis_input,
                voice=voice_params,
                audio_config=audio_config,
            )
            break
        except (
            google_exceptions.ServiceUnavailable,
            google_exceptions.ResourceExhausted,
            google_exceptions.DeadlineExceeded,
            ConnectionError,
            ConnectionResetError,
        ) as e:
            last_error = e
            if attempt < MAX_RETRIES - 1:
                delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                log_info(
                    f"TTS transient error (attempt {attempt + 1}/{MAX_RETRIES}): {e}. "
                    f"Retrying in {delay}s..."
                )
                time.sleep(delay)
            else:
                log_error(f"TTS failed after {MAX_RETRIES} attempts: {e}")
                output_error(f"TTS API failed after {MAX_RETRIES} retries: {e}")
        except Exception as e:
            log_error(f"TTS API error: {e}")
            log_error(traceback.format_exc())
            output_error(f"TTS API error: {e}")
    else:
        output_error(f"TTS API failed after {MAX_RETRIES} retries: {last_error}")

    # ---- Step 6: Write audio to output file ----
    audio_size = len(tts_response.audio_content)
    log_info(f"TTS response: {audio_size:,} bytes of audio")

    try:
        with open(output_mp3_path, "wb") as f:
            f.write(tts_response.audio_content)
        log_info(f"Audio written to {output_mp3_path}")
    except Exception as e:
        log_error(f"Failed to write audio file: {e}")
        output_error(f"Failed to write audio file: {e}")

    # ---- Return combined result ----
    output_result({
        "success": True,
        "text": detected_text,
        "char_count": char_count,
        "line_count": line_count,
        "audio_size": audio_size,
        "output_path": output_mp3_path,
        "voice_id": voice_id,
        "message": f"OCR+TTS complete: {char_count:,} chars, {audio_size:,} bytes",
    })


# ---------------------------------------------------------------------------
# Persistent worker mode: serve()
# ---------------------------------------------------------------------------
# When launched with `python3 gcp_worker.py serve`, the process stays alive
# and reads JSON commands from stdin, one per line. This eliminates the
# ~1.7s per-call overhead of Python startup + imports + client initialization
# by paying it once at startup. gRPC connections are also kept warm across
# requests, which can significantly reduce API call latency.
#
# Protocol:
#   Parent → Worker (stdin):  {"action": "ocr", "image_path": "/tmp/img.png"}\n
#   Worker → Parent (stdout): {"success": true, "text": "...", ...}\n
#   Ready signal (first line): {"ready": true}\n
#   Shutdown: {"action": "shutdown"}\n  or  close stdin (EOF)

def serve():
    """
    Persistent worker mode — reads JSON commands from stdin, dispatches to
    do_* functions with pre-initialized clients, writes JSON responses to stdout.

    Startup sequence:
      1. Reconfigure stdout for line buffering (ensures each JSON line is flushed)
      2. Read credentials from environment
      3. Import google-cloud libs and init both Vision + TTS clients (pay once)
      4. Send {"ready": true} to stdout
      5. Enter command loop

    The command loop runs until stdin is closed (EOF) or a {"action": "shutdown"}
    command is received.
    """
    # Step 1: Ensure stdout flushes after every line (critical for JSON protocol).
    # Without this, responses may sit in the buffer and the parent hangs waiting.
    sys.stdout.reconfigure(line_buffering=True)

    # Step 2: Read credentials from environment
    creds_b64 = os.environ.get("GCP_CREDENTIALS_BASE64", "")
    if not creds_b64:
        print(json.dumps({"ready": False, "message": "GCP_CREDENTIALS_BASE64 not set"}), flush=True)
        return

    # Step 3: Import all google-cloud libs and init clients upfront.
    # This is the ~1.5s we're paying ONCE instead of every call.
    try:
        log_info("serve: initializing clients...")
        creds_json = _decode_credentials(creds_b64)
        vision_client = init_vision_client(creds_b64, creds_json=creds_json)
        tts_client = init_tts_client(creds_b64, creds_json=creds_json)
        log_info("serve: both clients initialized")
    except Exception as e:
        log_error(f"serve: client init failed: {e}")
        print(json.dumps({"ready": False, "message": f"Client init failed: {e}"}), flush=True)
        return

    # Step 4: Signal to parent that we're ready to accept commands
    print(json.dumps({"ready": True}), flush=True)
    log_info("serve: ready, waiting for commands...")

    # Step 5: Command loop — read JSON from stdin, dispatch, write JSON to stdout
    while True:
        try:
            line = sys.stdin.readline()
        except (IOError, OSError):
            # Stdin pipe broken — parent closed it or crashed
            log_info("serve: stdin read error, exiting")
            break

        if not line:
            # EOF — parent closed stdin, time to exit gracefully
            log_info("serve: stdin closed (EOF), exiting")
            break

        line = line.strip()
        if not line:
            # Blank line — skip (shouldn't happen in normal operation)
            continue

        # Parse the command JSON
        try:
            cmd = json.loads(line)
        except json.JSONDecodeError as e:
            log_error(f"serve: invalid JSON: {e}")
            print(json.dumps({"success": False, "message": f"Invalid command JSON: {e}"}), flush=True)
            continue

        action = cmd.get("action", "")

        # Shutdown command — exit the loop cleanly
        if action == "shutdown":
            log_info("serve: shutdown command received, exiting")
            break

        # Dispatch to the appropriate action handler.
        # The do_* functions raise WorkerResult/WorkerError which we catch here
        # and write as JSON to stdout, then continue to the next command.
        try:
            if action == "ocr":
                do_ocr(cmd.get("image_path", ""), creds_b64,
                       vision_client=vision_client,
                       crop_region=cmd.get("crop_region"),
                       ocr_language=cmd.get("ocr_language"))

            elif action == "tts":
                do_tts(cmd.get("text", ""), cmd.get("output_path", ""),
                       cmd.get("voice_id", "en-US-Neural2-C"),
                       cmd.get("speech_rate", "medium"), creds_b64,
                       tts_client=tts_client)

            elif action == "ocr_tts":
                do_ocr_tts(cmd.get("image_path", ""), cmd.get("output_path", ""),
                           cmd.get("voice_id", "en-US-Neural2-C"),
                           cmd.get("speech_rate", "medium"), creds_b64,
                           vision_client=vision_client, tts_client=tts_client,
                           crop_region=cmd.get("crop_region"),
                           ocr_language=cmd.get("ocr_language"))

            else:
                print(json.dumps({"success": False, "message": f"Unknown action: {action}"}), flush=True)

        except WorkerResult as r:
            # Successful result from do_* function
            print(json.dumps(r.data), flush=True)

        except WorkerError as e:
            # Error result from do_* function
            print(json.dumps(e.data), flush=True)

        except Exception as e:
            # Unexpected error — log it and return an error response.
            # The worker stays alive; only individual requests fail.
            log_error(f"serve: unexpected error: {e}")
            log_error(traceback.format_exc())
            print(json.dumps({"success": False, "message": f"Worker error: {e}"}), flush=True)

    log_info("serve: exiting")


# ---------------------------------------------------------------------------
# Entry point — CLI mode (one-shot) dispatcher
# ---------------------------------------------------------------------------

def main():
    """
    Parse CLI arguments and dispatch to the appropriate action handler.

    Usage: python3 gcp_worker.py <action> [args...]

    Actions:
      ocr <image_path>                                              — Perform OCR on the given image file
      tts <text> <output_path> <voice_id> <rate>                     — Synthesize speech from text
      ocr_tts <image_path> <output_mp3_path> <voice_id> <rate>      — Combined OCR+TTS in one process
      serve                                                          — Persistent mode (stdin/stdout JSON)

    Credentials are read from the GCP_CREDENTIALS_BASE64 environment variable.
    """
    # Validate we got at least an action argument
    if len(sys.argv) < 2:
        # Can't use output_error here for "serve" since it raises an exception,
        # but for the CLI usage case, we wrap everything in try/except below.
        print(json.dumps({"success": False, "message": "Usage: gcp_worker.py <action> [args...]"}), flush=True)
        sys.exit(1)

    action = sys.argv[1]

    # "serve" mode has its own credential handling and loop — not wrapped
    # by the one-shot try/except below.
    if action == "serve":
        serve()
        return

    # Read credentials from environment (not CLI args — avoids ps exposure)
    creds_b64 = os.environ.get("GCP_CREDENTIALS_BASE64", "")
    if not creds_b64:
        print(json.dumps({"success": False, "message": "GCP_CREDENTIALS_BASE64 environment variable not set"}), flush=True)
        sys.exit(1)

    # Dispatch to the appropriate action handler.
    # Each do_* function raises WorkerResult or WorkerError instead of
    # writing to stdout directly. We catch those here and handle output + exit.
    try:
        if action == "ocr":
            if len(sys.argv) < 3:
                raise WorkerError("Usage: gcp_worker.py ocr <image_path>")
            do_ocr(sys.argv[2], creds_b64)

        elif action == "tts":
            if len(sys.argv) < 6:
                raise WorkerError("Usage: gcp_worker.py tts <text> <output_path> <voice_id> <speech_rate>")
            do_tts(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], creds_b64)

        elif action == "ocr_tts":
            if len(sys.argv) < 6:
                raise WorkerError("Usage: gcp_worker.py ocr_tts <image_path> <output_mp3_path> <voice_id> <speech_rate>")
            do_ocr_tts(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], creds_b64)

        else:
            raise WorkerError(f"Unknown action: {action}")

    except WorkerResult as r:
        # Success — print JSON and exit cleanly
        print(json.dumps(r.data), flush=True)
        sys.exit(0)

    except WorkerError as e:
        # Error — print JSON and exit with error code
        print(json.dumps(e.data), flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
