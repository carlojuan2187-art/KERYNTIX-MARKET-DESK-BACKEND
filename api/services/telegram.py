"""Minimal Telegram Bot API client used only for authentication and notifications."""
from __future__ import annotations

import os
from typing import Any

import httpx


class TelegramDeliveryError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = True, retry_after_seconds: int | None = None) -> None:
        super().__init__(message)
        self.retryable = bool(retryable)
        self.retry_after_seconds = max(0, int(retry_after_seconds)) if retry_after_seconds is not None else None


class TelegramBotAPI:
    def __init__(self, token: str | None = None, *, timeout_seconds: float = 8.0) -> None:
        self.token = token if token is not None else os.getenv("TELEGRAM_AUTH_BOT_TOKEN", "")
        self.timeout_seconds = timeout_seconds

    def send_message(self, chat_id: str | int, text: str, *, reply_markup: dict[str, Any] | None = None) -> None:
        if not self.token:
            raise TelegramDeliveryError("TELEGRAM_AUTH_BOT_TOKEN no está configurado", retryable=False)
        payload: dict[str, Any] = {"chat_id": str(chat_id), "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        try:
            response = httpx.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json=payload,
                timeout=self.timeout_seconds,
            )
            try:
                result = response.json()
            except ValueError:
                result = {}
            if response.status_code == 429 or result.get("error_code") == 429:
                parameters = result.get("parameters") if isinstance(result, dict) else {}
                retry_after = parameters.get("retry_after") if isinstance(parameters, dict) else None
                raise TelegramDeliveryError("Telegram rate limit", retryable=True, retry_after_seconds=retry_after)
            if response.status_code >= 500:
                raise TelegramDeliveryError("Telegram server error", retryable=True)
            if response.status_code >= 400:
                raise TelegramDeliveryError("Telegram rechazó el destinatario o el mensaje", retryable=False)
            if result.get("ok") is not True:
                error_code = result.get("error_code") if isinstance(result, dict) else None
                retryable = not isinstance(error_code, int) or error_code >= 500
                raise TelegramDeliveryError("Telegram rechazó el envío del mensaje", retryable=retryable)
        except TelegramDeliveryError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise TelegramDeliveryError("No se pudo entregar el mensaje por Telegram", retryable=True) from exc

    def bot_username(self) -> str:
        return os.getenv("TELEGRAM_AUTH_BOT_USERNAME", "").strip().lstrip("@")

    def is_configured(self) -> bool:
        username = self.bot_username()
        token = (self.token or "").strip().lower()
        username_norm = username.lower()
        placeholders = ("replace-with", "your_new", "placeholder", "change-me", "changeme")
        return bool(token and username and not any(marker in token for marker in placeholders) and not any(marker in username_norm for marker in placeholders))
