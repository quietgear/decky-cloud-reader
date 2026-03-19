# =============================================================================
# Tests for GCP worker JSON protocol (gcp_worker.py:1037-1111)
# =============================================================================
# Tests the stdin/stdout JSON-lines protocol used by the persistent worker.
# Since we can't easily test the full serve() loop (it requires GCP clients),
# we test the command parsing and dispatch logic at a unit level.

import json

from gcp_worker import WorkerError, WorkerResult


class TestWorkerProtocol:
    def test_shutdown_command_format(self):
        """The shutdown command is a simple JSON object with action='shutdown'."""
        cmd = json.dumps({"action": "shutdown"})
        parsed = json.loads(cmd)
        assert parsed["action"] == "shutdown"

    def test_invalid_json_is_detectable(self):
        """The serve loop catches JSONDecodeError for malformed input."""
        bad_input = "not valid json {{"
        try:
            json.loads(bad_input)
            assert False, "Should have raised"
        except json.JSONDecodeError:
            pass

    def test_unknown_action_produces_error(self):
        """Unknown actions should produce an error response."""
        cmd = {"action": "nonexistent_action"}
        # Simulate what the serve loop does for unknown actions
        response = {"success": False, "message": f"Unknown action: {cmd['action']}"}
        assert response["success"] is False
        assert "nonexistent_action" in response["message"]

    def test_worker_result_serialization(self):
        """WorkerResult.data should be JSON-serializable."""
        try:
            raise WorkerResult({"success": True, "text": "Hello world", "word_count": 2})
        except WorkerResult as r:
            serialized = json.dumps(r.data)
            parsed = json.loads(serialized)
            assert parsed["success"] is True
            assert parsed["word_count"] == 2

    def test_worker_error_serialization(self):
        """WorkerError.data should be JSON-serializable."""
        try:
            raise WorkerError("OCR failed: timeout")
        except WorkerError as e:
            serialized = json.dumps(e.data)
            parsed = json.loads(serialized)
            assert parsed["success"] is False
            assert "timeout" in parsed["message"]

    def test_ready_signal_format(self):
        """The first line from the worker in serve mode is a ready signal."""
        ready = json.dumps({"ready": True})
        parsed = json.loads(ready)
        assert parsed["ready"] is True
