from __future__ import annotations

from unittest.mock import patch

import pytest

from api.services.telegram import TelegramBotAPI, TelegramDeliveryError


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def test_telegram_429_exposes_retry_after():
    client = TelegramBotAPI(token="test-token")
    with patch("api.services.telegram.httpx.post", return_value=FakeResponse(429, {
        "ok": False, "error_code": 429, "parameters": {"retry_after": 17},
    })):
        with pytest.raises(TelegramDeliveryError) as caught:
            client.send_message("chat-1", "hello")
    assert caught.value.retryable is True
    assert caught.value.retry_after_seconds == 17


def test_telegram_400_is_permanent():
    client = TelegramBotAPI(token="test-token")
    with patch("api.services.telegram.httpx.post", return_value=FakeResponse(400, {
        "ok": False, "error_code": 400, "description": "chat not found",
    })):
        with pytest.raises(TelegramDeliveryError) as caught:
            client.send_message("bad-chat", "hello")
    assert caught.value.retryable is False


def test_telegram_500_is_retryable():
    client = TelegramBotAPI(token="test-token")
    with patch("api.services.telegram.httpx.post", return_value=FakeResponse(503, {"ok": False, "error_code": 503})):
        with pytest.raises(TelegramDeliveryError) as caught:
            client.send_message("chat-1", "hello")
    assert caught.value.retryable is True
