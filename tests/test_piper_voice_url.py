# =============================================================================
# Tests for _piper_voice_url() (main.py:227-250)
# =============================================================================
# Constructs HuggingFace download URLs for Piper TTS voice files. The URL
# structure must match the actual HuggingFace repo layout exactly.

from main import PIPER_VOICE_BASE_URL, _piper_voice_url


class TestPiperVoiceUrl:
    def test_standard_voice(self):
        url = _piper_voice_url("en_US-amy-medium")
        assert url == f"{PIPER_VOICE_BASE_URL}/en/en_US/amy/medium/en_US-amy-medium.onnx"

    def test_different_quality(self):
        url = _piper_voice_url("en_US-amy-low")
        assert url == f"{PIPER_VOICE_BASE_URL}/en/en_US/amy/low/en_US-amy-low.onnx"

    def test_multi_part_speaker_name(self):
        """Speaker names like 'ukrainian_tts' have a hyphen when split."""
        url = _piper_voice_url("uk_UA-ukrainian_tts-medium")
        assert url == f"{PIPER_VOICE_BASE_URL}/uk/uk_UA/ukrainian_tts/medium/uk_UA-ukrainian_tts-medium.onnx"

    def test_json_extension(self):
        url = _piper_voice_url("en_US-amy-medium", ext=".onnx.json")
        assert url.endswith("/en_US-amy-medium.onnx.json")

    def test_german_voice(self):
        url = _piper_voice_url("de_DE-thorsten-medium")
        assert "/de/de_DE/thorsten/medium/" in url

    def test_russian_voice(self):
        url = _piper_voice_url("ru_RU-denis-medium")
        assert "/ru/ru_RU/denis/medium/" in url

    def test_multi_hyphen_speaker(self):
        """Voices with multi-part speaker names like 'en_US-john-doe-medium'."""
        url = _piper_voice_url("en_US-john-doe-medium")
        # speaker should be "john-doe", quality should be "medium"
        assert "/en/en_US/john-doe/medium/en_US-john-doe-medium.onnx" in url
