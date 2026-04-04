from unittest.mock import patch, MagicMock

import pytest

from webhook import verify_signature, extract_message


class TestVerifySignature:
    @patch("webhook.TWILIO_AUTH_TOKEN", "test_auth_token")
    def test_valid_signature(self):
        mock_validator = MagicMock()
        mock_validator.validate.return_value = True

        with patch("webhook.RequestValidator", return_value=mock_validator):
            result = verify_signature(
                "https://example.com/webhook",
                {"Body": "almuerzo 32000"},
                "valid_signature",
            )

        assert result is True
        mock_validator.validate.assert_called_once_with(
            "https://example.com/webhook",
            {"Body": "almuerzo 32000"},
            "valid_signature",
        )

    @patch("webhook.TWILIO_AUTH_TOKEN", "test_auth_token")
    def test_invalid_signature(self):
        mock_validator = MagicMock()
        mock_validator.validate.return_value = False

        with patch("webhook.RequestValidator", return_value=mock_validator):
            result = verify_signature(
                "https://example.com/webhook",
                {"Body": "almuerzo 32000"},
                "bad_signature",
            )

        assert result is False


class TestExtractMessage:
    def _make_params(self, body="almuerzo 32000"):
        return {
            "From": "whatsapp:+573001234567",
            "Body": body,
            "MessageSid": "SMxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        }

    def test_extracts_message(self):
        result = extract_message(self._make_params())

        assert result == {
            "phone": "+573001234567",
            "text": "almuerzo 32000",
            "message_id": "SMxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        }

    def test_strips_whatsapp_prefix(self):
        result = extract_message(self._make_params())
        assert not result["phone"].startswith("whatsapp:")

    def test_strips_whitespace_from_text(self):
        result = extract_message(self._make_params("  almuerzo 32000  "))
        assert result["text"] == "almuerzo 32000"

    def test_returns_none_for_missing_fields(self):
        assert extract_message({}) is None
        assert extract_message({"From": "whatsapp:+573001234567"}) is None
        assert extract_message({"Body": "almuerzo", "MessageSid": "SM123"}) is None
