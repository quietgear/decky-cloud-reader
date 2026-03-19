# =============================================================================
# Tests for credential validation logic (main.py:3636-3714)
# =============================================================================
# Tests the load_credentials_file() RPC method's validation of GCP service
# account JSON files. Since it's an async method on Plugin, we test the
# validation logic by calling it through asyncio.

import asyncio
import json
from unittest.mock import MagicMock

from main import REQUIRED_GCP_FIELDS, Plugin


def _make_plugin_with_mock_settings():
    """Create a Plugin instance with mocked settings and worker management."""
    plugin = object.__new__(Plugin)
    plugin.settings = MagicMock()
    plugin._stop_worker = MagicMock()
    return plugin


def _run(coro):
    """Helper to run async functions in tests."""
    return asyncio.new_event_loop().run_until_complete(coro)


class TestLoadCredentialsFile:
    def test_valid_credentials(self, sample_gcp_credentials_file, sample_gcp_credentials):
        plugin = _make_plugin_with_mock_settings()
        result = _run(plugin.load_credentials_file(sample_gcp_credentials_file))

        assert result["valid"] is True
        assert "test-project-123" in result["message"]
        assert result["project_id"] == "test-project-123"
        # Should have stored the encoded credentials
        plugin.settings.set.assert_called_once()
        # Should have stopped the worker for credential refresh
        plugin._stop_worker.assert_called_once()

    def test_invalid_json(self, tmp_path):
        path = str(tmp_path / "bad.json")
        with open(path, "w") as f:
            f.write("NOT JSON {{{")

        plugin = _make_plugin_with_mock_settings()
        result = _run(plugin.load_credentials_file(path))

        assert result["valid"] is False
        assert "Invalid JSON" in result["message"]

    def test_missing_required_fields(self, tmp_path):
        path = str(tmp_path / "incomplete.json")
        with open(path, "w") as f:
            json.dump({"type": "service_account"}, f)

        plugin = _make_plugin_with_mock_settings()
        result = _run(plugin.load_credentials_file(path))

        assert result["valid"] is False
        assert "Missing required fields" in result["message"]

    def test_wrong_type_field(self, tmp_path):
        creds = {
            "type": "authorized_user",
            "project_id": "test",
            "private_key_id": "key",
            "private_key": "pk",
            "client_email": "email",
        }
        path = str(tmp_path / "wrong_type.json")
        with open(path, "w") as f:
            json.dump(creds, f)

        plugin = _make_plugin_with_mock_settings()
        result = _run(plugin.load_credentials_file(path))

        assert result["valid"] is False
        assert "service_account" in result["message"]

    def test_file_not_found(self):
        plugin = _make_plugin_with_mock_settings()
        result = _run(plugin.load_credentials_file("/nonexistent/path/creds.json"))

        assert result["valid"] is False
        assert "not found" in result["message"].lower() or "File not found" in result["message"]

    def test_required_fields_list(self):
        """Ensure REQUIRED_GCP_FIELDS contains the expected fields."""
        expected = {"type", "project_id", "private_key_id", "private_key", "client_email"}
        assert set(REQUIRED_GCP_FIELDS) == expected
