# =============================================================================
# Tests for Game Mode detection (_is_game_mode)
# =============================================================================
#
# The plugin should only react to button/touch triggers in Game Mode (Gamescope
# active). Detection is based on the existence of the Gamescope Wayland socket
# at /run/user/1000/gamescope-0 (the deck user on SteamOS).

import stat
from unittest.mock import patch

from main import Plugin


class TestIsGameMode:
    """Tests for Plugin._is_game_mode() static method."""

    def test_returns_true_when_gamescope_socket_exists(self, tmp_path):
        """Game Mode: Gamescope socket exists and is a socket file."""
        # Create a real Unix socket to test against
        import socket

        socket_path = tmp_path / "gamescope-0"
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(socket_path))
        sock.close()

        with patch("main.os.path.exists", return_value=True):
            with patch("main.os.stat") as mock_stat:
                mock_stat.return_value.st_mode = stat.S_IFSOCK | 0o755
                assert Plugin._is_game_mode() is True

    def test_returns_false_when_socket_missing(self):
        """Desktop Mode: Gamescope socket does not exist."""
        with patch("main.os.path.exists", return_value=False):
            assert Plugin._is_game_mode() is False

    def test_returns_false_when_path_is_regular_file(self):
        """Stale regular file at socket path should not count as Game Mode."""
        with patch("main.os.path.exists", return_value=True):
            with patch("main.os.stat") as mock_stat:
                # Regular file, not a socket
                mock_stat.return_value.st_mode = stat.S_IFREG | 0o644
                assert Plugin._is_game_mode() is False

    def test_returns_false_on_oserror(self):
        """OSError during stat (e.g., permission denied) returns False."""
        with patch("main.os.path.exists", return_value=True):
            with patch("main.os.stat", side_effect=OSError("Permission denied")):
                assert Plugin._is_game_mode() is False

    def test_checks_deck_user_path(self):
        """Socket path must use UID 1000 (deck user), not the current process UID."""
        with patch("main.os.path.exists", return_value=False) as mock_exists:
            Plugin._is_game_mode()
            mock_exists.assert_called_once_with("/run/user/1000/gamescope-0")
