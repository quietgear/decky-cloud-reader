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
# Entry point
# ---------------------------------------------------------------------------

def main():
    """
    Parse CLI arguments and dispatch to the appropriate action handler.

    Usage: python3 gcp_worker.py <action> [args...]

    Actions:
      ocr <image_path>  — Perform OCR on the given image file

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

    else:
        output_error(f"Unknown action: {action}")


if __name__ == "__main__":
    main()
