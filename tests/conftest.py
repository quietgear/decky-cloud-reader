# =============================================================================
# Test fixtures and mock setup for Decky Cloud Reader tests
# =============================================================================
#
# The `decky` module is injected at runtime by Decky Loader — it doesn't exist
# in normal Python environments. We inject a mock into sys.modules BEFORE any
# plugin code is imported, so all `import decky` calls resolve to our fake.

import json
import logging
import sys
import tempfile
import types

import pytest

# ---------------------------------------------------------------------------
# Mock decky module — injected before any plugin imports
# ---------------------------------------------------------------------------

mock_decky = types.ModuleType("decky")
mock_decky.logger = logging.getLogger("decky_test")
mock_decky.logger.setLevel(logging.DEBUG)
mock_decky.DECKY_PLUGIN_SETTINGS_DIR = tempfile.mkdtemp()
mock_decky.DECKY_PLUGIN_DIR = "/tmp/fake_plugin_dir"
mock_decky.DECKY_PLUGIN_LOG_DIR = "/tmp/fake_log_dir"


# decky.emit is an async function used by main.py for backend→frontend events.
# Provide a no-op coroutine so it doesn't blow up during tests.
async def _mock_emit(*args, **kwargs):
    pass


mock_decky.emit = _mock_emit

sys.modules["decky"] = mock_decky


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_settings_dir(tmp_path):
    """Provide a fresh temporary directory for SettingsManager tests."""
    return str(tmp_path)


@pytest.fixture
def sample_gcp_credentials():
    """Return a valid GCP service account JSON dict for testing."""
    return {
        "type": "service_account",
        "project_id": "test-project-123",
        "private_key_id": "key-id-abc",
        "private_key": "-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----\n",
        "client_email": "test@test-project-123.iam.gserviceaccount.com",
        "client_id": "123456789",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }


@pytest.fixture
def sample_gcp_credentials_file(tmp_path, sample_gcp_credentials):
    """Write a valid GCP service account JSON file and return its path."""
    path = tmp_path / "service-account.json"
    path.write_text(json.dumps(sample_gcp_credentials))
    return str(path)
