"""
tests/test_notifier.py
======================
Unit tests for core.notifier alert dispatch.
"""

from unittest.mock import patch, MagicMock
from core.notifier import send_alert, DEFAULT_TOPIC


def test_send_alert_success():
    with patch("requests.Session.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        ok = send_alert("Test Title", "Test Message", priority="urgent", tags=["rocket", "lock"])
        assert ok is True
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert args[0] == f"https://ntfy.sh/{DEFAULT_TOPIC}"
        assert kwargs["headers"]["Title"] == "Test Title"
        assert kwargs["headers"]["Priority"] == "urgent"
        assert kwargs["headers"]["Tags"] == "rocket,lock"
        assert kwargs["data"] == b"Test Message"
        assert kwargs["timeout"] == 3.0


def test_send_alert_failure_silent():
    with patch("requests.Session.post") as mock_post:
        mock_post.side_effect = Exception("Network timeout")

        ok = send_alert("Test Title", "Test Message")
        assert ok is False


def test_send_alert_custom_topic_and_str_tag():
    with patch("requests.Session.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        ok = send_alert("Title", "Msg", tags="warning", topic="custom-topic-123")
        assert ok is True
        args, kwargs = mock_post.call_args
        assert args[0] == "https://ntfy.sh/custom-topic-123"
        assert kwargs["headers"]["Tags"] == "warning"
