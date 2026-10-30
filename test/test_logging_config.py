from __future__ import annotations

import logging

from api.logging_config import TelegramTokenRedactionFilter, configure_logging


def test_httpx_summaries_remain_available_for_market_diagnostics(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "INFO")

    configure_logging()

    assert logging.getLogger("httpx").level == logging.INFO
    assert logging.getLogger("httpcore").level == logging.WARNING


def test_telegram_bot_token_is_redacted_from_httpx_request_url(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    configure_logging()
    record = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname="httpx/_client.py",
        lineno=100,
        msg='HTTP Request: POST https://api.telegram.org/bot123456:SECRET_TOKEN/sendMessage "HTTP/1.1 200 OK"',
        args=(),
        exc_info=None,
    )

    assert TelegramTokenRedactionFilter().filter(record) is True
    assert "SECRET_TOKEN" not in record.getMessage()
    assert "[REDACTED]" in record.getMessage()


def test_binance_request_url_remains_readable_in_httpx_log():
    record = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname="httpx/_client.py",
        lineno=100,
        msg='HTTP Request: GET https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT "HTTP/1.1 200 OK"',
        args=(),
        exc_info=None,
    )

    assert TelegramTokenRedactionFilter().filter(record) is True
    assert "fapi.binance.com" in record.getMessage()
    assert "[REDACTED]" not in record.getMessage()
