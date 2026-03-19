# =============================================================================
# Tests for _apply_text_filters() (main.py:2109-2159)
# =============================================================================
# The text filter runs between OCR and TTS in the pipeline. It strips unwanted
# words from OCR output before speech synthesis. Two independent modes:
#   - "Always": case-insensitive whole-word removal anywhere
#   - "Beginning": remove matching words from the first N tokens


from main import Plugin


def _make_plugin_with_settings(settings_dict):
    """
    Create a minimal mock object that has .settings as a dict-like,
    so _apply_text_filters can call self.settings.get().
    """
    plugin = object.__new__(Plugin)
    plugin.settings = settings_dict
    return plugin


class TestAlwaysFilter:
    def test_empty_text_returns_unchanged(self):
        p = _make_plugin_with_settings({"ignored_words_always_enabled": True, "ignored_words_always": "foo"})
        assert p._apply_text_filters("") == ""
        # Whitespace-only text returns the original (early return before filtering)
        assert p._apply_text_filters("   ") == "   "

    def test_removes_word_anywhere(self):
        p = _make_plugin_with_settings({"ignored_words_always_enabled": True, "ignored_words_always": "Chapter"})
        assert p._apply_text_filters("Chapter One begins here") == "One begins here"

    def test_case_insensitive(self):
        p = _make_plugin_with_settings({"ignored_words_always_enabled": True, "ignored_words_always": "hello"})
        assert p._apply_text_filters("HELLO world Hello") == "world"

    def test_whole_word_only(self):
        """Should not remove 'cat' from 'concatenate'."""
        p = _make_plugin_with_settings({"ignored_words_always_enabled": True, "ignored_words_always": "cat"})
        result = p._apply_text_filters("The cat sat on concatenate")
        assert "concatenate" in result
        assert result == "The sat on concatenate"

    def test_multiple_words(self):
        p = _make_plugin_with_settings(
            {
                "ignored_words_always_enabled": True,
                "ignored_words_always": "Chapter, Part",
            }
        )
        assert p._apply_text_filters("Chapter 1 Part 2") == "1 2"

    def test_special_chars_in_word(self):
        """re.escape should handle regex metacharacters in the ignore word."""
        p = _make_plugin_with_settings({"ignored_words_always_enabled": True, "ignored_words_always": "C++"})
        # \b doesn't match around + signs the same way, but re.escape prevents crashes
        p._apply_text_filters("Learn C++ programming")  # should not raise

    def test_disabled_does_nothing(self):
        p = _make_plugin_with_settings(
            {
                "ignored_words_always_enabled": False,
                "ignored_words_always": "Chapter",
            }
        )
        assert p._apply_text_filters("Chapter One") == "Chapter One"


class TestBeginningFilter:
    def test_removes_from_first_n_tokens(self):
        p = _make_plugin_with_settings(
            {
                "ignored_words_beginning_enabled": True,
                "ignored_words_beginning": "Chapter",
                "ignored_words_count": 3,
            }
        )
        assert p._apply_text_filters("Chapter One begins here") == "One begins here"

    def test_punctuation_tolerant(self):
        """'Chapter:' should match ignore word 'Chapter' after stripping punctuation."""
        p = _make_plugin_with_settings(
            {
                "ignored_words_beginning_enabled": True,
                "ignored_words_beginning": "Chapter",
                "ignored_words_count": 3,
            }
        )
        result = p._apply_text_filters("Chapter: The beginning")
        assert "Chapter" not in result

    def test_only_checks_first_n_words(self):
        p = _make_plugin_with_settings(
            {
                "ignored_words_beginning_enabled": True,
                "ignored_words_beginning": "end",
                "ignored_words_count": 2,
            }
        )
        # "end" appears at position 5, beyond the first 2 tokens
        result = p._apply_text_filters("the start middle fourth end")
        assert "end" in result

    def test_disabled_does_nothing(self):
        p = _make_plugin_with_settings(
            {
                "ignored_words_beginning_enabled": False,
                "ignored_words_beginning": "Chapter",
                "ignored_words_count": 3,
            }
        )
        assert p._apply_text_filters("Chapter One") == "Chapter One"


class TestBothFiltersCombined:
    def test_always_and_beginning_together(self):
        p = _make_plugin_with_settings(
            {
                "ignored_words_always_enabled": True,
                "ignored_words_always": "um",
                "ignored_words_beginning_enabled": True,
                "ignored_words_beginning": "Chapter",
                "ignored_words_count": 3,
            }
        )
        result = p._apply_text_filters("Chapter One um hello um world")
        assert "Chapter" not in result
        assert "um" not in result
        assert "hello" in result
        assert "world" in result

    def test_cleans_extra_whitespace(self):
        p = _make_plugin_with_settings({"ignored_words_always_enabled": True, "ignored_words_always": "the"})
        result = p._apply_text_filters("the big the bad the wolf")
        # After removing "the" three times, multiple spaces should be collapsed
        assert "  " not in result


class TestNoFiltersActive:
    def test_returns_original_when_no_filters(self):
        p = _make_plugin_with_settings({})
        assert p._apply_text_filters("Hello world") == "Hello world"

    def test_none_text_returns_none(self):
        p = _make_plugin_with_settings({})
        result = p._apply_text_filters(None)
        assert result is None
