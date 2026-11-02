from __future__ import annotations

import threading
from datetime import timedelta

from api.repositories.auth import InMemoryAuthRepository
from api.services.auth import hash_token, utcnow
from worker.telegram_auth_bot import TelegramAuthBot


class FakeRepository:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeHttpClient:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_auth_bot_stop_event_exits_without_polling_and_closes_resources():
    repository = FakeRepository()
    bot = TelegramAuthBot(repository, "123456:abcdefghijklmnopqrstuvwxyzABCDE123456", username="keryntix_market_desk_test_bot")
    client = FakeHttpClient()
    bot.client.close()
    bot.client = client
    stop_event = threading.Event()
    stop_event.set()

    bot.run_forever(stop_event)

    assert client.closed is True
    assert repository.closed is True


def test_auth_bot_accepts_registration_challenge_on_verify_deep_link():
    repository = InMemoryAuthRepository()
    token = "registration-challenge-token-for-bot-test"
    token_hash = hash_token(token)
    repository.create_auth_request({
        "token_hash": token_hash,
        "flow": "register",
        "phone": "+17875550123",
        "link_expires_at": utcnow() + timedelta(minutes=5),
        "contact_verified": False,
        "consumed": False,
    })
    bot = TelegramAuthBot(
        repository, "123456:abcdefghijklmnopqrstuvwxyzABCDE123456", username="keryntix_market_desk_test_bot"
    )
    sent = []
    bot.send = lambda chat_id, text, reply_markup=None: sent.append((str(chat_id), text, reply_markup))
    try:
        bot._start(
            {"chat": {"id": 4242, "type": "private"}, "from": {"id": 4242}},
            f"verify_{token}",
        )
        assert len(sent) == 1
        assert sent[0][0] == "4242"
        assert sent[0][2]["keyboard"][0][0]["request_contact"] is True
    finally:
        bot.client.close()
