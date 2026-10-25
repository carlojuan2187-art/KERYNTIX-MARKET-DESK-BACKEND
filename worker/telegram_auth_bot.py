"""Small standalone Telegram bot for phone verification and account linking.

Use a dedicated TELEGRAM_AUTH_BOT_TOKEN. This process must not share the old
bot's token/polling process. No chat handlers or strategy logic are imported.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import threading

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # Environment may be injected by the process manager.
    pass
import time
from typing import Any

import httpx

from api.repositories.auth import AuthRepository, auth_repository_from_environment
from api.services.auth import as_utc, handle_telegram_contact, hash_token, utcnow
from api.services.telegram import TelegramBotAPI
from api.cache import cache_from_environment
from worker.distributed_lease import DistributedWorkerLease

logger = logging.getLogger("ftrade.telegram_auth")
CONTACT_KEYBOARD = {
    "keyboard": [[{"text": "Compartir mi número de teléfono", "request_contact": True}]],
    "resize_keyboard": True,
    "one_time_keyboard": True,
    "is_persistent": False,
}
REMOVE_KEYBOARD = {"remove_keyboard": True}


class TelegramAuthBot:
    def __init__(self, repository: AuthRepository, token: str, *, username: str | None = None) -> None:
        if not token:
            raise RuntimeError("TELEGRAM_AUTH_BOT_TOKEN es obligatorio")
        self.repository = repository
        self.token = token
        self.username = username or os.getenv("TELEGRAM_AUTH_BOT_USERNAME", "")
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.client = httpx.Client(timeout=httpx.Timeout(40.0, connect=8.0))
        self.offset = 0

    def api(self, method: str, payload: dict[str, Any] | None = None, *, timeout: float = 12.0) -> Any:
        response = self.client.post(f"{self.base_url}/{method}", json=payload or {}, timeout=timeout)
        response.raise_for_status()
        body = response.json()
        if body.get("ok") is not True:
            raise RuntimeError(f"Telegram API error in {method}")
        return body.get("result")

    def send(self, chat_id: str | int, text: str, *, reply_markup: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"chat_id": str(chat_id), "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        self.api("sendMessage", payload)

    def _start(self, message: dict[str, Any], argument: str) -> None:
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        chat_id = str(chat.get("id", ""))
        if not chat_id or str(chat.get("type", "private")) != "private":
            return
        flow_prefix, sep, token = argument.partition("_")
        if not sep or flow_prefix not in {"verify", "link"} or not token:
            self.send(chat_id, "Para verificar tu acceso, abre el enlace de inicio desde la web de FTrade.")
            return
        token_hash = hash_token(token)
        challenge = self.repository.get_auth_request(token_hash)
        if not challenge:
            self.send(chat_id, "El enlace no es válido. Vuelve a la web de FTrade y solicita uno nuevo.")
            return
        expected_flow = "login" if flow_prefix == "verify" else "link"
        if challenge.get("flow") != expected_flow:
            self.send(chat_id, "Este enlace no corresponde a esta operación.")
            return
        expires = as_utc(challenge.get("link_expires_at"))
        if not expires or expires <= utcnow():
            self.send(chat_id, "El enlace venció. Vuelve a la web de FTrade y solicita uno nuevo.")
            return
        updated = self.repository.update_auth_request(token_hash, {
            "pending_chat_id": chat_id,
            "pending_telegram_user_id": str(sender.get("id", "")),
            "pending_started_at": utcnow(),
        }, conditions={"contact_verified": False, "link_expires_at_gt": utcnow()})
        if not updated:
            self.send(chat_id, "La solicitud ya venció o fue utilizada. Solicita una nueva desde FTrade.")
            return
        self.send(
            chat_id,
            "Para continuar con FTrade, comparte tu propio contacto usando el botón de abajo. Debe coincidir con el número que escribiste en la web.",
            reply_markup=CONTACT_KEYBOARD,
        )

    def _contact(self, message: dict[str, Any]) -> None:
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        contact = message.get("contact") or {}
        chat_id = str(chat.get("id", ""))
        challenge = self.repository.get_pending_request_for_chat(chat_id)
        if not challenge:
            self.send(chat_id, "No hay una solicitud pendiente para este chat. Abre de nuevo el enlace de FTrade.", reply_markup=REMOVE_KEYBOARD)
            return
        try:
            result = handle_telegram_contact(
                self.repository,
                token_hash=str(challenge["token_hash"]),
                sender_user_id=str(sender.get("id", "")),
                contact_user_id=str(contact.get("user_id")) if contact.get("user_id") is not None else None,
                chat_id=chat_id,
                contact_phone=str(contact.get("phone_number", "")),
                username=sender.get("username"),
            )
        except ValueError as exc:
            reason = str(exc)
            messages = {
                "AUTH_REQUEST_NOT_FOUND": "No encontré esa solicitud. Abre el enlace más reciente de FTrade.",
                "AUTH_REQUEST_EXPIRED": "La solicitud venció. Vuelve a la web de FTrade y comienza de nuevo.",
                "CONTACT_OWNER_MISMATCH": "Debes compartir tu propio contacto con el botón de Telegram, no reenviar el contacto de otra persona.",
                "CONTACT_ATTEMPTS_EXHAUSTED": "Se agotaron los intentos de verificación del contacto. Vuelve a la web de FTrade y solicita un nuevo enlace.",
                "PHONE_MISMATCH": "El número compartido no coincide con el que escribiste en la web. Comprueba el número e inténtalo otra vez.",
                "ACCOUNT_PHONE_MISMATCH": "El teléfono no coincide con esta cuenta de FTrade.",
                "TELEGRAM_ALREADY_LINKED": "Esta cuenta de Telegram ya está vinculada a otra cuenta de FTrade.",
            }
            self.send(chat_id, messages.get(reason, "No se pudo verificar el contacto. Vuelve a empezar desde la web de FTrade."), reply_markup=CONTACT_KEYBOARD)
            return
        except Exception:
            logger.exception("Fallo verificando contacto de Telegram")
            self.send(chat_id, "No se pudo completar la verificación. Inténtalo de nuevo desde FTrade.", reply_markup=REMOVE_KEYBOARD)
            return
        if result["flow"] == "link":
            self.send(chat_id, "Telegram quedó vinculado a tu cuenta de FTrade.", reply_markup=REMOVE_KEYBOARD)
        else:
            code = str(result["code"])
            self.send(chat_id, f"Tu código de acceso FTrade es: {code}\nVence en 5 minutos. No lo compartas.", reply_markup=REMOVE_KEYBOARD)

    def process_update(self, update: dict[str, Any]) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return
        text = str(message.get("text") or "")
        if text.startswith("/start"):
            pieces = text.split(maxsplit=1)
            self._start(message, pieces[1] if len(pieces) == 2 else "")
        elif isinstance(message.get("contact"), dict):
            self._contact(message)
        elif message.get("chat"):
            self.send(str(message["chat"].get("id", "")), "Abre el enlace de verificación desde FTrade para continuar.")

    def run_forever(self, stop_event: threading.Event | None = None) -> None:
        """Run polling until a stop event is signalled; close resources on every exit."""
        logger.info("Iniciando bot mínimo de autenticación FTrade; username=%s", self.username or "no configurado")
        try:
            while stop_event is None or not stop_event.is_set():
                try:
                    updates = self.api("getUpdates", {"offset": self.offset, "timeout": 30, "allowed_updates": ["message"]}, timeout=38.0) or []
                    for update in updates:
                        if stop_event is not None and stop_event.is_set():
                            break
                        self.offset = max(self.offset, int(update.get("update_id", 0)) + 1)
                        try:
                            self.process_update(update)
                        except Exception:
                            logger.exception("No se pudo procesar una actualización de Telegram")
                except KeyboardInterrupt:
                    break
                except Exception:
                    logger.exception("Error consultando Telegram; se reintentará")
                    if stop_event is not None:
                        if stop_event.wait(3):
                            break
                    else:
                        time.sleep(3)
        finally:
            self.client.close()
            close = getattr(self.repository, "close", None)
            if callable(close):
                close()


async def _run_worker() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    token = os.getenv("TELEGRAM_AUTH_BOT_TOKEN", "")
    token_norm = token.strip().lower()
    if not token or any(marker in token_norm for marker in ("replace-with", "placeholder", "change-me", "changeme")):
        raise RuntimeError("Configura TELEGRAM_AUTH_BOT_TOKEN con el token real del bot nuevo.")
    if not TelegramBotAPI(token=token).is_configured() or not os.getenv("TELEGRAM_AUTH_BOT_USERNAME", "").strip():
        raise RuntimeError("Configura TELEGRAM_AUTH_BOT_TOKEN y TELEGRAM_AUTH_BOT_USERNAME para el bot de autenticación.")

    repository = auth_repository_from_environment()
    storage = repository.health()
    if os.getenv("FTRADE_ENV", "development").strip().lower() == "production" and not storage.get("persistent"):
        repository.close()
        raise RuntimeError("El bot de autenticación requiere usuarios y solicitudes persistentes en MongoDB")
    cache = cache_from_environment()
    bot = TelegramAuthBot(repository, token)
    stop_thread = threading.Event()
    stop_async = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_async.set)
        except (NotImplementedError, RuntimeError):
            pass

    task: asyncio.Task[Any] | None = None
    try:
        async with DistributedWorkerLease(cache, "telegram-auth-bot"):
            task = asyncio.create_task(asyncio.to_thread(bot.run_forever, stop_thread), name="telegram-auth-bot-polling")
            logger.info("Bot de autenticación activo; storage=%s", storage.get("adapter"))
            while not stop_async.is_set():
                if task.done():
                    error = task.exception()
                    if error is not None:
                        raise RuntimeError("El loop de polling de Telegram terminó con error") from error
                    raise RuntimeError("El loop de polling de Telegram terminó inesperadamente")
                try:
                    await asyncio.wait_for(stop_async.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
    finally:
        stop_thread.set()
        if task is not None:
            # Keep the distributed lease until the long-poll request returns and
            # the polling thread closes its Mongo/http resources. Telegram's long
            # poll has a 38-second client timeout; this normally finishes promptly.
            try:
                await asyncio.shield(task)
            except Exception:
                logger.exception("El proceso de polling del bot terminó con error durante el cierre")
        else:
            bot.client.close()
            close = getattr(repository, "close", None)
            if callable(close):
                close()
        await cache.close()


def main() -> None:
    asyncio.run(_run_worker())


if __name__ == "__main__":
    main()
