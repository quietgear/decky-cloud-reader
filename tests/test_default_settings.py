# =============================================================================
# Tests for DEFAULT_SETTINGS (main.py:259-353)
# =============================================================================
# Ensures all expected settings keys are present, have correct types, and
# no values are accidentally None.

from main import DEFAULT_SETTINGS

# Every setting key that the plugin expects, with its expected Python type.
EXPECTED_KEYS_AND_TYPES = {
    "gcp_credentials_base64": str,
    "ocr_provider": str,
    "tts_provider": str,
    "voice_id": str,
    "speech_rate": str,
    "local_voice_id": str,
    "local_speech_rate": str,
    "ocr_language": str,
    "volume": int,
    "enabled": bool,
    "debug": bool,
    "trigger_button": str,
    "hold_time_ms": int,
    "touch_input_enabled": bool,
    "touch_input_style": str,
    "mute_interface_sounds": bool,
    "hide_pipeline_toast": bool,
    "show_text_overlay": bool,
    "fixed_region_x1": int,
    "fixed_region_y1": int,
    "fixed_region_x2": int,
    "fixed_region_y2": int,
    "last_selection_x1": int,
    "last_selection_y1": int,
    "last_selection_x2": int,
    "last_selection_y2": int,
    "ignored_words_always": str,
    "ignored_words_always_enabled": bool,
    "ignored_words_beginning": str,
    "ignored_words_beginning_enabled": bool,
    "ignored_words_count": int,
    "translation_enabled": bool,
    "translation_target_language": str,
}


class TestDefaultSettings:
    def test_all_expected_keys_present(self):
        for key in EXPECTED_KEYS_AND_TYPES:
            assert key in DEFAULT_SETTINGS, f"Missing key: {key}"

    def test_no_unexpected_keys(self):
        """Catch accidentally added keys that aren't tracked in tests."""
        for key in DEFAULT_SETTINGS:
            assert key in EXPECTED_KEYS_AND_TYPES, f"Unexpected key in DEFAULT_SETTINGS: {key}"

    def test_correct_types(self):
        for key, expected_type in EXPECTED_KEYS_AND_TYPES.items():
            value = DEFAULT_SETTINGS[key]
            assert isinstance(
                value, expected_type
            ), f"Key '{key}': expected {expected_type.__name__}, got {type(value).__name__} ({value!r})"

    def test_no_none_defaults(self):
        for key, value in DEFAULT_SETTINGS.items():
            assert value is not None, f"Key '{key}' has None default"

    def test_provider_defaults_are_local(self):
        assert DEFAULT_SETTINGS["ocr_provider"] == "local"
        assert DEFAULT_SETTINGS["tts_provider"] == "local"

    def test_full_screen_region_defaults(self):
        assert DEFAULT_SETTINGS["fixed_region_x1"] == 0
        assert DEFAULT_SETTINGS["fixed_region_y1"] == 0
        assert DEFAULT_SETTINGS["fixed_region_x2"] == 1280
        assert DEFAULT_SETTINGS["fixed_region_y2"] == 800
