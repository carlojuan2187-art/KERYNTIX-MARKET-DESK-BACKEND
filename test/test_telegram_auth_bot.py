from __future__ import annotations

import threading

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
    bot = TelegramAuthBot(repository, "123456:abcdefghijklmnopqrstuvwxyzABCDE123456", username="ftrade_test_bot")
    client = FakeHttpClient()
    bot.client.close()
    bot.client = client
    stop_event = threading.Event()
    stop_event.set()

    bot.run_forever(stop_event)

    assert client.closed is True
    assert repository.closed is True
