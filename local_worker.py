#!/usr/bin/env python3
# =============================================================================
# Decky Cloud Reader — Local Worker Subprocess
# =============================================================================
#
# This script runs under the BUNDLED Python 3.12 (python312/python/bin/python3.12),
# NOT the system Python 3.13 or Decky's embedded Python. It exists as a
# separate process because:
#   - RapidOCR (via onnxruntime) requires Python <3.13
#   - Piper TTS uses native C extensions that must match the interpreter
#   - We bundle a standalone Python 3.12 interpreter specifically for this
#
# Two operating modes (same protocol as gcp_worker.py):
#   1. CLI mode (one-shot): run a single action, output JSON, exit
#      python3.12 local_worker.py ocr /tmp/screenshot.png
#   2. Serve mode (persistent): stay alive, read JSON commands from stdin,
#      write JSON responses to stdout, reuse pre-initialized models
#      python3.12 local_worker.py serve
#
# Communication contract:
#   - CLI mode: CLI args for action + file paths; single JSON to stdout; exit
#   - Serve mode: JSON lines on stdin/stdout; first line is {"ready": true}
#   - Logs: All diagnostic messages go to stderr (picked up by parent process)
#
# Environment variables:
#   - LOCAL_MODELS_DIR: path to the models/ directory containing bundled OCR det/cls models
#   - LOCAL_OCR_MODELS_DIR: path to the settings ocr_models/ dir with per-language rec models
#   - LOCAL_VOICES_DIR: path to the settings voices/ dir with on-demand Piper TTS voices
#   - OMP_NUM_THREADS: set by this script to limit CPU usage (default: 2)
#
# IMPORTANT: This file must NOT import `decky` — it doesn't exist in bundled Python.
# =============================================================================

import sys
import os
import json
import time
import traceback
import wave
import struct
from io import BytesIO

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Prefix for all log messages so they can be identified in combined stderr output.
# Different from gcp_worker's "[DCR-worker]" to distinguish local subprocess logs.
WORKER_LOG = "[DCR-local]"

# Piper TTS speech rate mapping. Piper uses "length_scale" which is INVERSE:
# lower value = faster speech, higher value = slower speech.
# This is the opposite of GCP TTS where higher speaking_rate = faster.
PIPER_RATE_MAP = {
    "x-slow": 1.6,
    "slow": 1.3,
    "medium": 1.0,
    "fast": 0.8,
    "x-fast": 0.6,
}

# Default voice used when no voice_id is specified.
DEFAULT_PIPER_VOICE = "en_US-amy-medium"


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
# Exception-based flow control (same pattern as gcp_worker.py)
# ---------------------------------------------------------------------------
# The do_* action functions use these exceptions to deliver results back to
# the dispatcher (CLI main() or persistent serve() loop). This allows the
# same do_* functions to work in both one-shot CLI mode and long-running
# serve mode.

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

    In CLI mode, the exception propagates to main() which prints JSON and exits.
    In serve mode, the exception is caught by the command loop which writes JSON
    to stdout and continues.
    """
    raise WorkerResult(data)


def output_error(message):
    """
    Deliver an error result by raising WorkerError.

    Same propagation pattern as output_result().
    """
    raise WorkerError(message)


# ---------------------------------------------------------------------------
# Image cropping helper (Phase 12 capture modes)
# ---------------------------------------------------------------------------

def _crop_image(img, crop_region):
    """
    Crop a PIL Image to the specified bounding box.

    Coordinates are clamped to image bounds and normalized (min/max swap).
    Returns the original image if the crop region is too small (< 10px in
    either dimension) to avoid accidental touches producing garbage OCR.

    Args:
        img: PIL Image object.
        crop_region: Dict with keys "x1", "y1", "x2", "y2" (pixel coordinates).

    Returns:
        Cropped PIL Image, or the original if the region is too small.
    """
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
        return img

    log_info(f"Cropping image to ({x1},{y1})-({x2},{y2}) = {x2 - x1}x{y2 - y1}")
    return img.crop((x1, y1, x2, y2))


# ---------------------------------------------------------------------------
# OCR action — text detection using RapidOCR (offline)
# ---------------------------------------------------------------------------

def do_ocr(image_path, ocr_engine=None, crop_region=None, ocr_language=None):
    """
    Perform OCR on an image file using RapidOCR (offline, local inference).

    Steps:
      1. Read the image file from disk
      2. Crop to region if specified (Phase 12)
      3. Initialize the RapidOCR engine (skip if pre-initialized in serve mode)
      4. Run OCR on the image
      5. Sort text regions top-to-bottom, left-to-right
      6. Concatenate into a single text string

    Args:
        image_path: Absolute path to the screenshot PNG file.
        ocr_engine: Optional pre-initialized RapidOCR engine. When provided
                    (in serve mode), skips engine creation for speed.
        crop_region: Optional dict {"x1", "y1", "x2", "y2"} defining the
                    bounding box to crop before OCR. Coordinates are clamped
                    to image bounds and normalized (min/max swap). If None
                    or absent, the full image is used.
        ocr_language: OCR language identifier (e.g., "english"). Used to
                     initialize the engine with the correct rec model if
                     ocr_engine is None (CLI mode).

    Returns:
        Never returns — raises WorkerResult or WorkerError.
    """
    # Step 1: Read and validate the image file
    if not os.path.exists(image_path):
        output_error(f"Image file not found: {image_path}")

    from PIL import Image

    try:
        img = Image.open(image_path)
        # Convert to RGB if necessary (RapidOCR expects RGB or grayscale)
        if img.mode == "RGBA":
            img = img.convert("RGB")
        log_info(f"Loaded image: {img.size[0]}x{img.size[1]} ({img.mode})")
    except Exception as e:
        log_error(f"Failed to load image: {e}")
        output_error(f"Failed to load image: {e}")

    # Step 1b: Crop to region if specified (Phase 12 capture modes)
    if crop_region:
        img = _crop_image(img, crop_region)

    # Step 2: Initialize OCR engine if not pre-initialized
    if ocr_engine is None:
        try:
            ocr_engine = _init_ocr_engine(language_id=ocr_language)
        except Exception as e:
            log_error(f"Failed to init OCR engine: {e}")
            output_error(f"Failed to initialize OCR engine: {e}")

    # Step 3: Run OCR — rapidocr_onnxruntime returns a tuple-like result.
    # result[0] is a list of [bounding_box, text, confidence] items (or None).
    # result[1] is timing info.
    import numpy as np
    img_array = np.array(img)

    log_info(f"Running OCR on {img.size[0]}x{img.size[1]} image...")
    t_start = time.monotonic()

    result = ocr_engine(img_array)

    t_elapsed = time.monotonic() - t_start
    log_info(f"OCR completed in {t_elapsed:.2f}s")

    # Step 4: Handle case where no text is detected
    if not result or not result[0]:
        log_info("No text detected in image")
        output_result({
            "success": True,
            "text": "",
            "char_count": 0,
            "line_count": 0,
            "message": "No text detected in image",
        })

    # Step 5: Sort results top-to-bottom by the Y coordinate of the first
    # point of each bounding box. This gives a natural reading order.
    # Each item is [bounding_box, text, confidence].
    # bounding_box is a list of 4 [x,y] points.
    sorted_results = sorted(result[0], key=lambda item: item[0][0][1])

    # Step 6: Concatenate text from all detected regions
    detected_text = "\n".join(item[1] for item in sorted_results)
    char_count = len(detected_text)
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
# TTS action — synthesize speech using Piper TTS (offline)
# ---------------------------------------------------------------------------

def do_tts(text, output_path, speech_rate, voice_id=None, voice_cache=None, speaker_id=None):
    """
    Synthesize speech from text using Piper TTS (offline, local inference).

    Steps:
      1. Validate text input
      2. Load Piper voice (from cache in serve mode, or fresh in CLI mode)
      3. Map speech rate to Piper's length_scale
      4. Synthesize audio to WAV file

    Args:
        text: The text to synthesize into speech.
        output_path: Absolute path where the WAV file will be written.
        speech_rate: Speed preset from PIPER_RATE_MAP (e.g., "medium").
        voice_id: Piper voice to use (e.g., "en_US-ryan-medium"). Falls back
                  to DEFAULT_PIPER_VOICE if None or unknown.
        voice_cache: Dict of cached PiperVoice instances (serve mode). When None
                     (CLI mode), voice is loaded fresh and not cached.
        speaker_id: Integer speaker index for multi-speaker voices (e.g., 1).
                    When None, Piper defaults to speaker 0 for multi-speaker
                    voices. Only used with synthesize(), not synthesize_wav().

    Returns:
        Never returns — raises WorkerResult or WorkerError.
    """
    # Step 1: Validate text input
    if not text or not text.strip():
        output_error("No text provided for TTS")

    log_info(f"TTS input: {len(text):,} chars, voice={voice_id}, rate={speech_rate}")

    # Step 2: Load Piper voice — use cache in serve mode, fresh load in CLI mode
    try:
        if voice_cache is not None:
            # Serve mode: use lazy cache (loads on first use, reuses after)
            piper_voice, resolved_voice_id = _get_or_load_voice(voice_id, voice_cache)
        else:
            # CLI mode: load fresh (no cache to store it in)
            piper_voice, resolved_voice_id = _init_piper_voice(voice_id)
    except Exception as e:
        log_error(f"Failed to init Piper voice: {e}")
        output_error(f"Failed to initialize Piper voice: {e}")

    # Step 3: Map speech rate to Piper's length_scale (inverse: lower = faster)
    length_scale = PIPER_RATE_MAP.get(speech_rate, 1.0)
    if speech_rate not in PIPER_RATE_MAP:
        log_info(f"Unknown speech rate '{speech_rate}', defaulting to length_scale=1.0")

    # Step 4: Synthesize audio using piper-tts v1.3+ API.
    # synthesize_wav() writes a complete WAV file directly — simpler than
    # collecting raw PCM chunks manually. SynthesisConfig controls speech rate.
    log_info(f"Synthesizing speech with '{resolved_voice_id}' (length_scale={length_scale}, speaker_id={speaker_id})...")
    t_start = time.monotonic()

    try:
        # SynthesisConfig accepts both length_scale and speaker_id (for
        # multi-speaker voices). speaker_id is None for single-speaker voices.
        from piper.voice import SynthesisConfig
        syn_config = SynthesisConfig(length_scale=length_scale, speaker_id=speaker_id)

        with wave.open(output_path, "wb") as wav_file:
            piper_voice.synthesize_wav(text, wav_file, syn_config=syn_config)

        t_elapsed = time.monotonic() - t_start
        audio_size = os.path.getsize(output_path)
        log_info(f"Synthesis completed in {t_elapsed:.2f}s, WAV: {audio_size:,} bytes")

        output_result({
            "success": True,
            "audio_size": audio_size,
            "output_path": output_path,
            "text_length": len(text),
            "voice_id": resolved_voice_id,
            "message": f"TTS complete: {audio_size:,} bytes, piper/{resolved_voice_id}",
        })

    except (WorkerResult, WorkerError):
        # Re-raise flow-control exceptions so the serve loop can handle them
        raise
    except Exception as e:
        log_error(f"TTS synthesis error: {e}")
        log_error(traceback.format_exc())
        output_error(f"TTS synthesis error: {e}")


# ---------------------------------------------------------------------------
# Combined OCR+TTS action — single invocation for the pipeline
# ---------------------------------------------------------------------------

def do_ocr_tts(image_path, output_audio_path, speech_rate,
               ocr_engine=None, voice_id=None, voice_cache=None, speaker_id=None,
               crop_region=None, ocr_language=None):
    """
    Perform OCR and TTS in a single invocation (same as GCP's combined action).

    Steps:
      1. Read image, crop if region specified (Phase 12), and run OCR
      2. If text found, synthesize speech
      3. Return combined result

    Args:
        image_path: Absolute path to the screenshot PNG file.
        output_audio_path: Absolute path where the WAV file will be written.
        speech_rate: Speed preset from PIPER_RATE_MAP (e.g., "medium").
        ocr_engine: Optional pre-initialized RapidOCR engine (serve mode).
        voice_id: Piper voice to use (e.g., "en_US-ryan-medium"). Falls back
                  to DEFAULT_PIPER_VOICE if None or unknown.
        voice_cache: Dict of cached PiperVoice instances (serve mode). When None
                     (CLI mode), voice is loaded fresh and not cached.
        speaker_id: Integer speaker index for multi-speaker voices (e.g., 1).
                    When None, Piper defaults to speaker 0 for multi-speaker voices.
        crop_region: Optional dict {"x1", "y1", "x2", "y2"} defining the
                    bounding box to crop before OCR. If None, full image is used.
        ocr_language: OCR language identifier (e.g., "english"). Used to
                     initialize the engine with the correct rec model if
                     ocr_engine is None (CLI mode).

    Returns:
        Never returns — raises WorkerResult or WorkerError.
    """
    # ---- Step 1: Read and validate the image ----
    if not os.path.exists(image_path):
        output_error(f"Image file not found: {image_path}")

    from PIL import Image
    import numpy as np

    try:
        img = Image.open(image_path)
        if img.mode == "RGBA":
            img = img.convert("RGB")
        log_info(f"Loaded image: {img.size[0]}x{img.size[1]} ({img.mode})")
    except Exception as e:
        log_error(f"Failed to load image: {e}")
        output_error(f"Failed to load image: {e}")

    # Crop to region if specified (Phase 12 capture modes)
    if crop_region:
        img = _crop_image(img, crop_region)

    # ---- Step 2: Initialize OCR engine if needed ----
    if ocr_engine is None:
        try:
            ocr_engine = _init_ocr_engine(language_id=ocr_language)
        except Exception as e:
            log_error(f"Failed to init OCR engine: {e}")
            output_error(f"Failed to initialize OCR engine: {e}")

    # ---- Step 3: Run OCR (rapidocr_onnxruntime returns tuple-like result) ----
    img_array = np.array(img)
    log_info(f"Running OCR on {img.size[0]}x{img.size[1]} image...")
    t_ocr_start = time.monotonic()

    result = ocr_engine(img_array)

    t_ocr = time.monotonic() - t_ocr_start
    log_info(f"OCR completed in {t_ocr:.2f}s")

    # ---- Step 4: Handle no text detected ----
    # Resolve voice_id early so it appears in the result even when no text is found
    resolved_voice_id = voice_id if voice_id else DEFAULT_PIPER_VOICE

    if not result or not result[0]:
        log_info("No text detected in image")
        output_result({
            "success": True,
            "text": "",
            "char_count": 0,
            "line_count": 0,
            "audio_size": 0,
            "output_path": "",
            "voice_id": resolved_voice_id,
            "message": "No text detected in image",
        })

    # ---- Step 5: Sort and concatenate text ----
    sorted_results = sorted(result[0], key=lambda item: item[0][0][1])
    detected_text = "\n".join(item[1] for item in sorted_results)
    char_count = len(detected_text)
    line_count = detected_text.count("\n") + 1 if detected_text else 0
    log_info(f"OCR detected {char_count:,} chars, {line_count} lines")

    # ---- Step 6: Load Piper voice (from cache or fresh) ----
    try:
        if voice_cache is not None:
            piper_voice, resolved_voice_id = _get_or_load_voice(voice_id, voice_cache)
        else:
            piper_voice, resolved_voice_id = _init_piper_voice(voice_id)
    except Exception as e:
        log_error(f"Failed to init Piper voice: {e}")
        output_error(f"TTS voice init failed (OCR text available): {e}")

    # ---- Step 7: Synthesize speech (piper-tts v1.3+ API) ----
    length_scale = PIPER_RATE_MAP.get(speech_rate, 1.0)
    if speech_rate not in PIPER_RATE_MAP:
        log_info(f"Unknown speech rate '{speech_rate}', defaulting to length_scale=1.0")

    log_info(f"Synthesizing speech: {len(detected_text):,} chars, length_scale={length_scale}, speaker_id={speaker_id}")
    t_tts_start = time.monotonic()

    try:
        from piper.voice import SynthesisConfig
        syn_config = SynthesisConfig(length_scale=length_scale, speaker_id=speaker_id)

        with wave.open(output_audio_path, "wb") as wav_file:
            piper_voice.synthesize_wav(detected_text, wav_file, syn_config=syn_config)

        t_tts = time.monotonic() - t_tts_start
        audio_size = os.path.getsize(output_audio_path)
        log_info(f"Synthesis completed in {t_tts:.2f}s, WAV: {audio_size:,} bytes")

    except Exception as e:
        log_error(f"TTS synthesis error: {e}")
        log_error(traceback.format_exc())
        output_error(f"TTS synthesis error: {e}")

    # ---- Return combined result ----
    output_result({
        "success": True,
        "text": detected_text,
        "char_count": char_count,
        "line_count": line_count,
        "audio_size": audio_size,
        "output_path": output_audio_path,
        "voice_id": resolved_voice_id,
        "message": f"OCR+TTS complete: {char_count:,} chars, {audio_size:,} bytes, piper/{resolved_voice_id}",
    })


# ---------------------------------------------------------------------------
# Model initialization helpers
# ---------------------------------------------------------------------------

def _init_ocr_engine(language_id=None):
    """
    Initialize the RapidOCR engine with language-specific recognition model.

    Detection and classification models use rapidocr-onnxruntime's built-in
    defaults (bundled inside the pip package). Only the recognition model is
    swapped per language — downloaded on demand to LOCAL_OCR_MODELS_DIR/{language_id}/.

    Why built-in det/cls instead of custom models:
      - rapidocr-onnxruntime's post-processing expects its own det model format
      - PP-OCRv5 det models have a different architecture that produces garbage
        bounding boxes with rapidocr-onnxruntime (over-segmentation, 50+ "lines"
        from a small image)
      - The built-in det/cls models are proven to work and well-tested

    Model files:
      - Built-in detection model (inside rapidocr-onnxruntime package)
      - Built-in classification model (inside rapidocr-onnxruntime package)
      - LOCAL_OCR_MODELS_DIR/{language_id}/rec.onnx   (recognition, per-language)
      - LOCAL_OCR_MODELS_DIR/{language_id}/dict.txt   (character dictionary, per-language)

    Args:
        language_id: OCR language identifier (e.g., "english", "chinese").
                     Defaults to "english" if None.

    Returns:
        An initialized RapidOCR engine ready to process images.
    """
    if not language_id:
        language_id = "english"

    ocr_models_dir = os.environ.get("LOCAL_OCR_MODELS_DIR", "")
    if not ocr_models_dir:
        raise RuntimeError("LOCAL_OCR_MODELS_DIR environment variable not set")

    # Language-specific models: recognition + dictionary (downloaded on demand)
    lang_dir = os.path.join(ocr_models_dir, language_id)
    rec_path = os.path.join(lang_dir, "rec.onnx")
    dict_path = os.path.join(lang_dir, "dict.txt")

    # Verify rec model files exist before attempting to load
    for path, name in [(rec_path, "rec"), (dict_path, "dict")]:
        if not os.path.exists(path):
            raise FileNotFoundError(f"OCR model file not found: {path} ({name})")

    from rapidocr_onnxruntime import RapidOCR

    log_info(f"Initializing RapidOCR engine: language={language_id}")
    log_info(f"  det: built-in (rapidocr-onnxruntime default)")
    log_info(f"  cls: built-in (rapidocr-onnxruntime default)")
    log_info(f"  rec: {rec_path}")
    log_info(f"  dict: {dict_path}")

    # Only override rec model + dictionary. Det and cls use library defaults
    # which are compatible with rapidocr-onnxruntime's post-processing.
    engine = RapidOCR(
        rec_model_path=rec_path,
        rec_keys_path=dict_path,
    )
    log_info(f"RapidOCR engine initialized for language={language_id}")
    return engine


def _init_piper_voice(voice_id=None):
    """
    Load a Piper TTS voice model by voice_id.

    Voice models are stored in LOCAL_VOICES_DIR (the settings dir's voices/
    subdirectory) — they're downloaded on demand by main.py, not bundled in
    the plugin zip. This function accepts any voice_id as long as the files
    exist on disk (no validation against a hardcoded list).

    Args:
        voice_id: The voice identifier (e.g., "en_US-amy-medium"). Falls back
                  to DEFAULT_PIPER_VOICE if None or empty.

    Returns:
        A tuple of (PiperVoice, resolved_voice_id) — the resolved ID may differ
        from input if the requested voice was None/empty.
    """
    # Resolve voice_id — fall back to default if empty
    if not voice_id:
        voice_id = DEFAULT_PIPER_VOICE

    voices_dir = os.environ.get("LOCAL_VOICES_DIR", "")
    if not voices_dir:
        raise RuntimeError("LOCAL_VOICES_DIR environment variable not set")

    model_path = os.path.join(voices_dir, f"{voice_id}.onnx")
    config_path = os.path.join(voices_dir, f"{voice_id}.onnx.json")

    if not os.path.exists(model_path):
        raise FileNotFoundError(f"Piper voice model not found: {model_path}")
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Piper voice config not found: {config_path}")

    from piper import PiperVoice

    log_info(f"Loading Piper voice '{voice_id}' from {model_path}")
    voice = PiperVoice.load(model_path, config_path=config_path)

    # Check for multi-speaker voice — log it so we know speaker_id is needed
    num_speakers = getattr(voice.config, "num_speakers", 1)
    if num_speakers > 1:
        log_info(f"Piper voice '{voice_id}' is multi-speaker ({num_speakers} speakers, using speaker 0)")

    log_info(f"Piper voice '{voice_id}' loaded")
    return voice, voice_id


def _get_or_load_voice(voice_id, voice_cache):
    """
    Get a Piper voice from cache, or load and cache it on first use.

    This implements lazy-loading: voices are only loaded into memory when first
    requested, then cached in voice_cache dict for instant reuse. This way the
    worker doesn't pay the ~1s load cost for voices that are never used, and
    switching between previously-used voices is free.

    Accepts any voice_id — no validation against a hardcoded list. The caller
    (main.py) ensures the voice files exist before sending commands.

    Args:
        voice_id: The voice identifier (e.g., "en_US-ryan-medium").
        voice_cache: Dict mapping voice_id → PiperVoice (mutated in place).

    Returns:
        A tuple of (PiperVoice, resolved_voice_id).
    """
    # Resolve to default if empty
    resolved_id = voice_id if voice_id else DEFAULT_PIPER_VOICE

    if resolved_id in voice_cache:
        log_debug(f"Using cached voice '{resolved_id}'")
        return voice_cache[resolved_id], resolved_id

    log_info(f"Voice cache miss for '{resolved_id}', loading...")
    voice, resolved_id = _init_piper_voice(resolved_id)
    voice_cache[resolved_id] = voice
    log_info(f"Voice '{resolved_id}' loaded and cached ({len(voice_cache)} voice(s) in cache)")
    return voice, resolved_id


# ---------------------------------------------------------------------------
# Persistent worker mode: serve()
# ---------------------------------------------------------------------------
# Same protocol as gcp_worker.py:
#   Parent → Worker (stdin):  {"action": "ocr", "image_path": "/tmp/img.png"}\n
#   Worker → Parent (stdout): {"success": true, "text": "...", ...}\n
#   Ready signal (first line): {"ready": true}\n
#   Shutdown: {"action": "shutdown"}\n  or  close stdin (EOF)

def serve():
    """
    Persistent worker mode — reads JSON commands from stdin, dispatches to
    do_* functions with pre-initialized models, writes JSON responses to stdout.

    Startup sequence:
      1. Limit CPU threads (OMP_NUM_THREADS=2 for Steam Deck's 4 cores)
      2. Reconfigure stdout for line buffering
      3. Initialize lazy caches for OCR engine and TTS voices
      4. Send {"ready": true} to stdout
      5. Enter command loop

    OCR engine is lazily initialized on first OCR command and cached. Only one
    engine is kept in memory at a time — when the OCR language changes, the old
    engine is discarded and a new one is created. This avoids accumulating
    multiple ~100-200 MB engines in memory on the Steam Deck.
    """
    # Step 1: Limit CPU threads — leave cores for the game.
    # Must be set BEFORE importing onnxruntime (which reads it at import time).
    os.environ["OMP_NUM_THREADS"] = os.environ.get("OMP_NUM_THREADS", "2")

    # Step 2: Ensure stdout flushes after every line (critical for JSON protocol).
    sys.stdout.reconfigure(line_buffering=True)

    # Log environment directories so we can verify in debug output
    voices_dir = os.environ.get("LOCAL_VOICES_DIR", "")
    ocr_models_dir = os.environ.get("LOCAL_OCR_MODELS_DIR", "")
    log_info(f"serve: voices dir = {voices_dir}")
    log_info(f"serve: ocr_models dir = {ocr_models_dir}")

    # Step 3: Initialize lazy caches. OCR engine is NOT initialized upfront
    # (Phase 25) — it's created on first OCR command with the requested language.
    # This avoids loading a model that may not match the user's language setting.
    ocr_engine = None             # Lazily initialized RapidOCR engine
    ocr_engine_language = None    # Language ID of the cached engine
    voice_cache = {}              # Lazy voice cache: voice_id → PiperVoice
    log_info("serve: caches empty (lazy-load OCR engine + voices)")

    # Step 4: Signal to parent that we're ready to accept commands
    print(json.dumps({"ready": True}), flush=True)
    log_info("serve: ready, waiting for commands...")

    # Helper: get or reinitialize OCR engine for the requested language.
    # Returns the engine, or raises an exception on failure.
    def _get_ocr_engine(language_id):
        nonlocal ocr_engine, ocr_engine_language
        if not language_id:
            language_id = "english"
        # Reuse cached engine if language matches
        if ocr_engine is not None and ocr_engine_language == language_id:
            log_debug(f"Using cached OCR engine for language={language_id}")
            return ocr_engine
        # Language changed or no engine yet — (re)initialize
        if ocr_engine is not None:
            log_info(f"OCR language changed: {ocr_engine_language} → {language_id}, reinitializing...")
        else:
            log_info(f"Initializing OCR engine for language={language_id}...")
        ocr_engine = _init_ocr_engine(language_id=language_id)
        ocr_engine_language = language_id
        return ocr_engine

    # Step 5: Command loop — read JSON from stdin, dispatch, write JSON to stdout
    while True:
        try:
            line = sys.stdin.readline()
        except (IOError, OSError):
            log_info("serve: stdin read error, exiting")
            break

        if not line:
            # EOF — parent closed stdin, time to exit gracefully
            log_info("serve: stdin closed (EOF), exiting")
            break

        line = line.strip()
        if not line:
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
        # For OCR actions, lazily init/reinit the engine based on ocr_language.
        try:
            if action == "ocr":
                engine = _get_ocr_engine(cmd.get("ocr_language"))
                do_ocr(cmd.get("image_path", ""), ocr_engine=engine,
                       crop_region=cmd.get("crop_region"),
                       ocr_language=cmd.get("ocr_language"))

            elif action == "tts":
                do_tts(cmd.get("text", ""), cmd.get("output_path", ""),
                       cmd.get("speech_rate", "medium"),
                       voice_id=cmd.get("voice_id"),
                       voice_cache=voice_cache,
                       speaker_id=cmd.get("speaker_id"))

            elif action == "ocr_tts":
                engine = _get_ocr_engine(cmd.get("ocr_language"))
                do_ocr_tts(cmd.get("image_path", ""), cmd.get("output_path", ""),
                           cmd.get("speech_rate", "medium"),
                           ocr_engine=engine,
                           voice_id=cmd.get("voice_id"),
                           voice_cache=voice_cache,
                           speaker_id=cmd.get("speaker_id"),
                           crop_region=cmd.get("crop_region"),
                           ocr_language=cmd.get("ocr_language"))

            else:
                print(json.dumps({"success": False, "message": f"Unknown action: {action}"}), flush=True)

        except WorkerResult as r:
            print(json.dumps(r.data), flush=True)

        except WorkerError as e:
            print(json.dumps(e.data), flush=True)

        except Exception as e:
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

    Usage: python3.12 local_worker.py <action> [args...]

    Actions:
      ocr <image_path>                                            — Perform local OCR
      tts <text> <output_path> [rate] [voice_id]                  — Synthesize local TTS
      ocr_tts <image_path> <output_audio_path> [rate] [voice_id]  — Combined OCR+TTS
      serve                                                       — Persistent mode (stdin/stdout JSON)
    """
    if len(sys.argv) < 2:
        print(json.dumps({"success": False, "message": "Usage: local_worker.py <action> [args...]"}), flush=True)
        sys.exit(1)

    action = sys.argv[1]

    # Limit CPU threads for one-shot mode too
    os.environ["OMP_NUM_THREADS"] = os.environ.get("OMP_NUM_THREADS", "2")

    # "serve" mode has its own init and loop
    if action == "serve":
        serve()
        return

    # One-shot actions — each do_* function raises WorkerResult/WorkerError
    try:
        if action == "ocr":
            if len(sys.argv) < 3:
                raise WorkerError("Usage: local_worker.py ocr <image_path>")
            do_ocr(sys.argv[2])

        elif action == "tts":
            if len(sys.argv) < 4:
                raise WorkerError("Usage: local_worker.py tts <text> <output_path> [speech_rate] [voice_id]")
            speech_rate = sys.argv[4] if len(sys.argv) > 4 else "medium"
            voice_id = sys.argv[5] if len(sys.argv) > 5 else None
            do_tts(sys.argv[2], sys.argv[3], speech_rate, voice_id=voice_id)

        elif action == "ocr_tts":
            if len(sys.argv) < 4:
                raise WorkerError("Usage: local_worker.py ocr_tts <image_path> <output_audio_path> [speech_rate] [voice_id]")
            speech_rate = sys.argv[4] if len(sys.argv) > 4 else "medium"
            voice_id = sys.argv[5] if len(sys.argv) > 5 else None
            do_ocr_tts(sys.argv[2], sys.argv[3], speech_rate, voice_id=voice_id)

        else:
            raise WorkerError(f"Unknown action: {action}")

    except WorkerResult as r:
        print(json.dumps(r.data), flush=True)
        sys.exit(0)

    except WorkerError as e:
        print(json.dumps(e.data), flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
