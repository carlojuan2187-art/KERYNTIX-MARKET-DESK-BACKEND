from __future__ import annotations

from datetime import datetime, timedelta, timezone

from api.repositories.auth import InMemoryAuthRepository
from api.services.plans import PlanMaintenanceService

UTC = timezone.utc


class FakeTelegram:
    def __init__(self, fail_first=False):
        self.sent = []
        self.fail_first = fail_first

    def send_message(self, chat_id, text, *, reply_markup=None):
        if self.fail_first:
            self.fail_first = False
            raise RuntimeError("temporary Telegram failure")
        self.sent.append((str(chat_id), text))


def seed(repository, *, trial_end=None, plan="free", plan_end=None, chat="500"):
    repository.create_user_if_absent({
        "user_id": "usr_notice", "phone": "+17875550055", "telegram_chat_id": chat,
        "plan": plan, "trial_end": trial_end, "plan_end": plan_end, "ref_code": "ref_usr_notice",
    })


def test_upcoming_trial_notice_is_sent_once():
    repository = InMemoryAuthRepository()
    now = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
    seed(repository, trial_end=now + timedelta(hours=2))
    telegram = FakeTelegram()
    service = PlanMaintenanceService(repository, telegram, warning_hours=24)

    first = service.run_once(now=now)
    second = service.run_once(now=now + timedelta(minutes=1))

    assert first["sent"] == 1
    assert second["sent"] == 0
    assert second["deduplicated"] == 1
    assert len(telegram.sent) == 1


def test_expired_trial_notice_is_sent_when_scheduler_runs_late():
    repository = InMemoryAuthRepository()
    now = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
    seed(repository, trial_end=now - timedelta(hours=2))
    telegram = FakeTelegram()
    result = PlanMaintenanceService(repository, telegram).run_once(now=now)
    assert result["sent"] == 1
    assert "ha terminado" in telegram.sent[0][1]


def test_failed_telegram_delivery_is_retried_next_cycle():
    repository = InMemoryAuthRepository()
    now = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
    seed(repository, plan="premium", plan_end=now + timedelta(hours=3))
    telegram = FakeTelegram(fail_first=True)
    service = PlanMaintenanceService(repository, telegram, warning_hours=24)

    first = service.run_once(now=now)
    second = service.run_once(now=now + timedelta(minutes=1))

    assert first["failed"] == 1
    assert second["sent"] == 1
    assert len(telegram.sent) == 1


def test_no_notice_is_attempted_without_telegram_link():
    repository = InMemoryAuthRepository()
    now = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
    seed(repository, trial_end=now + timedelta(hours=2), chat=None)
    telegram = FakeTelegram()
    result = PlanMaintenanceService(repository, telegram).run_once(now=now)
    assert result["skipped_unlinked"] == 1
    assert result["sent"] == 0
    assert telegram.sent == []
