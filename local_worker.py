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
#   - LOCAL_MODELS_DIR: path to the models/ directory containing OCR + TTS models
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
# OCR action — text detection using RapidOCR (offline)
# ---------------------------------------------------------------------------

def do_ocr(image_path, ocr_engine=None):
    """
    Perform OCR on an image file using RapidOCR (offline, local inference).

    Steps:
      1. Read the image file from disk
      2. Initialize the RapidOCR engine (skip if pre-initialized in serve mode)
      3. Run OCR on the image
      4. Sort text regions top-to-bottom, left-to-right
      5. Concatenate into a single text string

    Args:
        image_path: Absolute path to the screenshot PNG file.
        ocr_engine: Optional pre-initialized RapidOCR engine. When provided
                    (in serve mode), skips engine creation for speed.

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

    # Step 2: Initialize OCR engine if not pre-initialized
    if ocr_engine is None:
        try:
            ocr_engine = _init_ocr_engine()
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

def do_tts(text, output_path, speech_rate, piper_voice=None):
    """
    Synthesize speech from text using Piper TTS (offline, local inference).

    Steps:
      1. Validate text input
      2. Initialize Piper voice model (skip if pre-initialized)
      3. Map speech rate to Piper's length_scale
      4. Synthesize audio to WAV file

    Args:
        text: The text to synthesize into speech.
        output_path: Absolute path where the WAV file will be written.
        speech_rate: Speed preset from PIPER_RATE_MAP (e.g., "medium").
        piper_voice: Optional pre-initialized PiperVoice. When provided
                     (in serve mode), skips model loading for speed.

    Returns:
        Never returns — raises WorkerResult or WorkerError.
    """
    # Step 1: Validate text input
    if not text or not text.strip():
        output_error("No text provided for TTS")

    log_info(f"TTS input: {len(text):,} chars, rate={speech_rate}")

    # Step 2: Initialize Piper voice if not pre-initialized
    if piper_voice is None:
        try:
            piper_voice = _init_piper_voice()
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
    log_info(f"Synthesizing speech (length_scale={length_scale})...")
    t_start = time.monotonic()

    try:
        from piper.voice import SynthesisConfig
        syn_config = SynthesisConfig(length_scale=length_scale)

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
            "voice_id": "en_US-lessac-medium",
            "message": f"TTS complete: {audio_size:,} bytes, piper/en_US-lessac-medium",
        })

    except Exception as e:
        log_error(f"TTS synthesis error: {e}")
        log_error(traceback.format_exc())
        output_error(f"TTS synthesis error: {e}")


# ---------------------------------------------------------------------------
# Combined OCR+TTS action — single invocation for the pipeline
# ---------------------------------------------------------------------------

def do_ocr_tts(image_path, output_audio_path, speech_rate,
               ocr_engine=None, piper_voice=None):
    """
    Perform OCR and TTS in a single invocation (same as GCP's combined action).

    Steps:
      1. Read image and run OCR
      2. If text found, synthesize speech
      3. Return combined result

    Args:
        image_path: Absolute path to the screenshot PNG file.
        output_audio_path: Absolute path where the WAV file will be written.
        speech_rate: Speed preset from PIPER_RATE_MAP (e.g., "medium").
        ocr_engine: Optional pre-initialized RapidOCR engine (serve mode).
        piper_voice: Optional pre-initialized PiperVoice (serve mode).

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

    # ---- Step 2: Initialize OCR engine if needed ----
    if ocr_engine is None:
        try:
            ocr_engine = _init_ocr_engine()
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
    if not result or not result[0]:
        log_info("No text detected in image")
        output_result({
            "success": True,
            "text": "",
            "char_count": 0,
            "line_count": 0,
            "audio_size": 0,
            "output_path": "",
            "voice_id": "en_US-lessac-medium",
            "message": "No text detected in image",
        })

    # ---- Step 5: Sort and concatenate text ----
    sorted_results = sorted(result[0], key=lambda item: item[0][0][1])
    detected_text = "\n".join(item[1] for item in sorted_results)
    char_count = len(detected_text)
    line_count = detected_text.count("\n") + 1 if detected_text else 0
    log_info(f"OCR detected {char_count:,} chars, {line_count} lines")

    # ---- Step 6: Initialize Piper voice if needed ----
    if piper_voice is None:
        try:
            piper_voice = _init_piper_voice()
        except Exception as e:
            log_error(f"Failed to init Piper voice: {e}")
            output_error(f"TTS voice init failed (OCR text available): {e}")

    # ---- Step 7: Synthesize speech (piper-tts v1.3+ API) ----
    length_scale = PIPER_RATE_MAP.get(speech_rate, 1.0)
    if speech_rate not in PIPER_RATE_MAP:
        log_info(f"Unknown speech rate '{speech_rate}', defaulting to length_scale=1.0")

    log_info(f"Synthesizing speech: {len(detected_text):,} chars, length_scale={length_scale}")
    t_tts_start = time.monotonic()

    try:
        from piper.voice import SynthesisConfig
        syn_config = SynthesisConfig(length_scale=length_scale)

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
        "voice_id": "en_US-lessac-medium",
        "message": f"OCR+TTS complete: {char_count:,} chars, {audio_size:,} bytes",
    })


# ---------------------------------------------------------------------------
# Model initialization helpers
# ---------------------------------------------------------------------------

def _init_ocr_engine():
    """
    Initialize the RapidOCR engine with downloaded ONNX model files.

    Same approach as Decky-Translator: pass custom model paths for the ONNX
    files (det, rec, cls) but do NOT pass rec_keys_path. The library's
    built-in character dictionary is guaranteed to match the recognition
    model's output vocabulary. Passing a separately downloaded keys file
    causes IndexError due to model-dictionary version mismatch.

    Model files are expected in LOCAL_MODELS_DIR/ocr/:
      - ch_PP-OCRv4_det_infer.onnx  (text detection)
      - ch_PP-OCRv4_rec_infer.onnx  (text recognition)
      - ch_ppocr_mobile_v2.0_cls_infer.onnx  (text direction classification)

    Returns:
        An initialized RapidOCR engine ready to process images.
    """
    models_dir = os.environ.get("LOCAL_MODELS_DIR", "")
    if not models_dir:
        raise RuntimeError("LOCAL_MODELS_DIR environment variable not set")

    ocr_dir = os.path.join(models_dir, "ocr")
    det_path = os.path.join(ocr_dir, "ch_PP-OCRv4_det_infer.onnx")
    rec_path = os.path.join(ocr_dir, "ch_PP-OCRv4_rec_infer.onnx")
    cls_path = os.path.join(ocr_dir, "ch_ppocr_mobile_v2.0_cls_infer.onnx")

    # Verify model files exist before attempting to load
    for path, name in [(det_path, "det"), (rec_path, "rec"), (cls_path, "cls")]:
        if not os.path.exists(path):
            raise FileNotFoundError(f"OCR model file not found: {path} ({name})")

    from rapidocr_onnxruntime import RapidOCR

    log_info(f"Initializing RapidOCR engine from {ocr_dir}")
    engine = RapidOCR(
        det_model_path=det_path,
        rec_model_path=rec_path,
        cls_model_path=cls_path,
        # NO rec_keys_path — use library's built-in keys (avoids mismatch)
    )
    log_info("RapidOCR engine initialized")
    return engine


def _init_piper_voice():
    """
    Load the Piper TTS voice model.

    Model files are expected in LOCAL_MODELS_DIR/tts/:
      - en_US-lessac-medium.onnx      (the voice model)
      - en_US-lessac-medium.onnx.json  (model config with sample_rate etc.)

    Returns:
        An initialized PiperVoice ready to synthesize speech.
    """
    models_dir = os.environ.get("LOCAL_MODELS_DIR", "")
    if not models_dir:
        raise RuntimeError("LOCAL_MODELS_DIR environment variable not set")

    tts_dir = os.path.join(models_dir, "tts")
    model_path = os.path.join(tts_dir, "en_US-lessac-medium.onnx")
    config_path = os.path.join(tts_dir, "en_US-lessac-medium.onnx.json")

    if not os.path.exists(model_path):
        raise FileNotFoundError(f"Piper voice model not found: {model_path}")
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Piper voice config not found: {config_path}")

    from piper import PiperVoice

    log_info(f"Loading Piper voice from {model_path}")
    voice = PiperVoice.load(model_path, config_path=config_path)
    log_info("Piper voice loaded")
    return voice


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
      3. Initialize RapidOCR engine + Piper voice model (pay once)
      4. Send {"ready": true} to stdout
      5. Enter command loop
    """
    # Step 1: Limit CPU threads — leave cores for the game.
    # Must be set BEFORE importing onnxruntime (which reads it at import time).
    os.environ["OMP_NUM_THREADS"] = os.environ.get("OMP_NUM_THREADS", "2")

    # Step 2: Ensure stdout flushes after every line (critical for JSON protocol).
    sys.stdout.reconfigure(line_buffering=True)

    # Step 3: Initialize OCR engine and Piper voice model upfront.
    # This is the ~3-5s we're paying ONCE instead of every call.
    try:
        log_info("serve: initializing models...")
        ocr_engine = _init_ocr_engine()
        piper_voice = _init_piper_voice()
        log_info("serve: both models initialized")
    except Exception as e:
        log_error(f"serve: model init failed: {e}")
        log_error(traceback.format_exc())
        print(json.dumps({"ready": False, "message": f"Model init failed: {e}"}), flush=True)
        return

    # Step 4: Signal to parent that we're ready to accept commands
    print(json.dumps({"ready": True}), flush=True)
    log_info("serve: ready, waiting for commands...")

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

        # Dispatch to the appropriate action handler
        try:
            if action == "ocr":
                do_ocr(cmd.get("image_path", ""), ocr_engine=ocr_engine)

            elif action == "tts":
                do_tts(cmd.get("text", ""), cmd.get("output_path", ""),
                       cmd.get("speech_rate", "medium"),
                       piper_voice=piper_voice)

            elif action == "ocr_tts":
                do_ocr_tts(cmd.get("image_path", ""), cmd.get("output_path", ""),
                           cmd.get("speech_rate", "medium"),
                           ocr_engine=ocr_engine, piper_voice=piper_voice)

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
      ocr <image_path>                                    — Perform local OCR
      tts <text> <output_path> [rate]                     — Synthesize local TTS
      ocr_tts <image_path> <output_audio_path> [rate]     — Combined OCR+TTS
      serve                                               — Persistent mode (stdin/stdout JSON)
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
                raise WorkerError("Usage: local_worker.py tts <text> <output_path> [speech_rate]")
            speech_rate = sys.argv[4] if len(sys.argv) > 4 else "medium"
            do_tts(sys.argv[2], sys.argv[3], speech_rate)

        elif action == "ocr_tts":
            if len(sys.argv) < 4:
                raise WorkerError("Usage: local_worker.py ocr_tts <image_path> <output_audio_path> [speech_rate]")
            speech_rate = sys.argv[4] if len(sys.argv) > 4 else "medium"
            do_ocr_tts(sys.argv[2], sys.argv[3], speech_rate)

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
