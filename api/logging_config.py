"""Central logging policy for the API and standalone workers.

HTTPX request summaries are useful when diagnosing Binance/data-provider latency.
Telegram embeds its reusable bot token in the request URL, so that one path is
redacted before the request summary reaches Railway logs.
"""
from __future__ import annotations

import logging
import os
import re


class TelegramTokenRedactionFilter(logging.Filter):
    """Redact Telegram Bot API tokens from HTTP request log messages."""

    _telegram_url = re.compile(
        r"(https?://api\.telegram\.org/bot)[^/\s?'\"<>]+",
        re.IGNORECASE,
    )

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        redacted = self._telegram_url.sub(r"\1[REDACTED]", message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def configure_logging() -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(level=level)

    # Keep HTTPX request summaries (for example Binance calls) but never allow the
    # Telegram bot token embedded in the URL path to reach Railway's log stream.
    httpx_logger = logging.getLogger("httpx")
    if not any(isinstance(item, TelegramTokenRedactionFilter) for item in httpx_logger.filters):
        httpx_logger.addFilter(TelegramTokenRedactionFilter())
    httpx_logger.setLevel(logging.INFO)

    # HTTPcore's low-level DEBUG output is too verbose; HTTPX provides the useful
    # request/response summary and warnings/errors remain visible from HTTPcore.
    logging.getLogger("httpcore").setLevel(logging.WARNING)
