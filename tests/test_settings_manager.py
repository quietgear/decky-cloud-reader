# =============================================================================
# Tests for SettingsManager (main.py:377-438)
# =============================================================================
# SettingsManager handles JSON persistence of plugin settings. These tests
# verify CRUD operations, file creation, corrupt file recovery, and missing
# file handling.

import json
import os

from main import SettingsManager


class TestSettingsManagerInit:
    def test_creates_path_from_name_and_directory(self, tmp_settings_dir):
        sm = SettingsManager("settings", tmp_settings_dir)
        assert sm.settings_path == os.path.join(tmp_settings_dir, "settings.json")

    def test_starts_with_empty_settings(self, tmp_settings_dir):
        sm = SettingsManager("settings", tmp_settings_dir)
        assert sm.settings == {}


class TestSettingsManagerReadWrite:
    def test_read_missing_file_stays_empty(self, tmp_settings_dir):
        sm = SettingsManager("settings", tmp_settings_dir)
        sm.read()
        assert sm.settings == {}

    def test_set_and_get(self, tmp_settings_dir):
        sm = SettingsManager("settings", tmp_settings_dir)
        sm.set("key", "value")
        assert sm.get("key") == "value"

    def test_get_missing_key_returns_default(self, tmp_settings_dir):
        sm = SettingsManager("settings", tmp_settings_dir)
        assert sm.get("nonexistent") is None
        assert sm.get("nonexistent", "fallback") == "fallback"

    def test_set_persists_to_disk(self, tmp_settings_dir):
        sm = SettingsManager("settings", tmp_settings_dir)
        sm.set("foo", 42)

        # Read the file directly
        with open(sm.settings_path) as f:
            data = json.load(f)
        assert data["foo"] == 42

    def test_read_loads_from_disk(self, tmp_settings_dir):
        # Write a settings file manually
        path = os.path.join(tmp_settings_dir, "settings.json")
        with open(path, "w") as f:
            json.dump({"loaded": True, "count": 5}, f)

        sm = SettingsManager("settings", tmp_settings_dir)
        sm.read()
        assert sm.get("loaded") is True
        assert sm.get("count") == 5

    def test_set_creates_directory_if_missing(self, tmp_path):
        nested_dir = str(tmp_path / "deeply" / "nested")
        sm = SettingsManager("settings", nested_dir)
        result = sm.set("key", "value")
        assert result is True
        assert os.path.exists(sm.settings_path)

    def test_get_all_returns_copy(self, tmp_settings_dir):
        sm = SettingsManager("settings", tmp_settings_dir)
        sm.set("a", 1)
        sm.set("b", 2)
        all_settings = sm.get_all()
        assert all_settings == {"a": 1, "b": 2}
        # Mutating the copy should not affect the original
        all_settings["c"] = 3
        assert sm.get("c") is None


class TestSettingsManagerCorruptFile:
    def test_read_corrupt_json_resets_to_empty(self, tmp_settings_dir):
        path = os.path.join(tmp_settings_dir, "settings.json")
        with open(path, "w") as f:
            f.write("NOT VALID JSON {{{")

        sm = SettingsManager("settings", tmp_settings_dir)
        sm.read()
        assert sm.settings == {}

    def test_set_returns_true_on_success(self, tmp_settings_dir):
        sm = SettingsManager("settings", tmp_settings_dir)
        assert sm.set("key", "value") is True
