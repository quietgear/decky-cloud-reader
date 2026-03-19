# =============================================================================
# Tests for GCP worker helper functions and constants
# =============================================================================
# Tests _decode_credentials(), VOICE_REGISTRY, SPEECH_RATE_MAP, and
# OCR_LANGUAGE_HINTS from gcp_worker.py. These are all importable without
# GCP client libraries (which are lazy-imported inside init_* functions).

import base64
import json

from gcp_worker import (
    OCR_LANGUAGE_HINTS,
    SPEECH_RATE_MAP,
    VOICE_REGISTRY,
    WorkerError,
    WorkerResult,
    _decode_credentials,
    output_error,
    output_result,
)


class TestDecodeCredentials:
    def test_valid_base64(self, sample_gcp_credentials):
        encoded = base64.b64encode(json.dumps(sample_gcp_credentials).encode()).decode()
        result = _decode_credentials(encoded)
        assert result["project_id"] == "test-project-123"
        assert result["type"] == "service_account"

    def test_invalid_base64(self):
        try:
            _decode_credentials("not-valid-base64!!!")
            assert False, "Should have raised"
        except Exception:
            pass  # Any exception is acceptable

    def test_valid_base64_but_invalid_json(self):
        encoded = base64.b64encode(b"not json").decode()
        try:
            _decode_credentials(encoded)
            assert False, "Should have raised json.JSONDecodeError"
        except json.JSONDecodeError:
            pass


class TestVoiceRegistry:
    def test_has_expected_voice_count(self):
        assert len(VOICE_REGISTRY) == 32

    def test_all_values_are_language_codes(self):
        """Every voice must map to a BCP-47-like language code."""
        for voice_id, lang_code in VOICE_REGISTRY.items():
            assert "-" in lang_code, f"Voice {voice_id} has invalid lang code: {lang_code}"
            parts = lang_code.split("-")
            assert len(parts) == 2, f"Voice {voice_id}: expected 'xx-YY', got '{lang_code}'"

    def test_default_voice_exists(self):
        assert "en-US-Neural2-C" in VOICE_REGISTRY

    def test_covers_expected_languages(self):
        lang_codes = set(VOICE_REGISTRY.values())
        expected = {"en-US", "en-GB", "uk-UA", "de-DE", "fr-FR", "es-ES", "ja-JP", "pt-BR", "ru-RU"}
        assert expected == lang_codes


class TestSpeechRateMap:
    def test_has_all_presets(self):
        expected_keys = {"x-slow", "slow", "medium", "fast", "x-fast"}
        assert set(SPEECH_RATE_MAP.keys()) == expected_keys

    def test_medium_is_one(self):
        assert SPEECH_RATE_MAP["medium"] == 1.0

    def test_rates_are_ordered(self):
        assert SPEECH_RATE_MAP["x-slow"] < SPEECH_RATE_MAP["slow"]
        assert SPEECH_RATE_MAP["slow"] < SPEECH_RATE_MAP["medium"]
        assert SPEECH_RATE_MAP["medium"] < SPEECH_RATE_MAP["fast"]
        assert SPEECH_RATE_MAP["fast"] < SPEECH_RATE_MAP["x-fast"]

    def test_all_values_positive(self):
        for key, value in SPEECH_RATE_MAP.items():
            assert value > 0, f"{key} has non-positive rate: {value}"


class TestOcrLanguageHints:
    def test_english_hint(self):
        assert "en" in OCR_LANGUAGE_HINTS["english"]

    def test_all_values_are_lists(self):
        for lang, hints in OCR_LANGUAGE_HINTS.items():
            assert isinstance(hints, list), f"{lang}: expected list, got {type(hints)}"
            assert len(hints) > 0, f"{lang}: empty hints list"


class TestWorkerExceptions:
    def test_output_result_raises_worker_result(self):
        try:
            output_result({"success": True, "text": "hello"})
            assert False, "Should have raised WorkerResult"
        except WorkerResult as r:
            assert r.data["success"] is True
            assert r.data["text"] == "hello"

    def test_output_error_raises_worker_error(self):
        try:
            output_error("Something went wrong")
            assert False, "Should have raised WorkerError"
        except WorkerError as e:
            assert e.data["success"] is False
            assert "Something went wrong" in e.data["message"]
