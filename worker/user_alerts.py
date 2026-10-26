"""Price/global-market alert dispatcher for FTrade Web.

It reads the shared market cache produced by worker.market_feed, persists the
user inbox, and publishes a Redis event for a future WebSocket/SSE adapter. It
never opens its own Binance connection.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping

from api.cache import JsonCache, cache_from_environment
from api.repositories.auth import AuthRepository, auth_repository_from_environment
from api.repositories.user_features import UserFeatureRepository, user_feature_repository_from_environment
from api.services.market import QUOTES_KEY
from api.services.telegram import TelegramBotAPI
from api.services.user_features import alert_preferences, wants_alert
from api.services.auth import is_admin_user
from domain.plans import evaluate_access

logger = logging.getLogger("ftrade.user_alerts")
NOTIFICATION_CHANNEL = "notifications:events:v1"
DISPATCH_STATUS_KEY = "user-alerts:status:v1"
GLOBAL_ALERT_STATE_KEY = "market-alerts:dispatcher-state:v1"
MAJORS = {"BTCUSDT", "ETHUSDT"}


def _num(value: Any) -> float | None:
    try:
        import math
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


class UserAlertProcessor:
    def __init__(self, auth_repository: AuthRepository, feature_repository: UserFeatureRepository,
                 cache: JsonCache, telegram_gateway: Any | None = None, *, clock=time.time,
                 max_telegram_per_cycle: int = 20, market_intelligence: Any | None = None) -> None:
        self.auth = auth_repository
        self.features = feature_repository
        self.cache = cache
        self.telegram = telegram_gateway or TelegramBotAPI()
        self.clock = clock
        self.max_telegram_per_cycle = max(0, min(100, int(max_telegram_per_cycle)))
        self.market_intelligence = market_intelligence
        self._telegram_sent = 0

    async def _refresh_global_alert_snapshots(self) -> None:
        """Keep global market alert evaluation independent of frontend traffic.

        The same cached services used by the API provide the source snapshots. Failures
        are logged and degrade to existing cache, never to synthetic market values.
        """
        if self.market_intelligence is None:
            return
        tasks = (
            ("liquidations-5m", self.market_intelligence.liquidations(5)),
            ("movers-5m", self.market_intelligence.movers(5)),
            ("movers-15m", self.market_intelligence.movers(15)),
        )
        results = await asyncio.gather(*(task for _name, task in tasks), return_exceptions=True)
        for (name, _task), result in zip(tasks, results):
            if isinstance(result, Exception):
                logger.warning("No se pudo refrescar snapshot para alertas (%s): %s", name, result)
                continue
            if not name.startswith("movers-") or not isinstance(result, dict):
                continue
            version = result.get("updated_epoch") or result.get("updated_at")
            window = result.get("window")
            if not version or window not in (5, 15, 60):
                continue
            marker_key = f"market:movers:last-published:v1:{int(window)}"
            try:
                previous = await self.cache.get_json(marker_key)
                if previous == version:
                    continue
                # Publish first and record the version second: if Redis publishing
                # fails, the next cycle will retry the same snapshot.
                await self.cache.publish_json("market:movers:events:v1", {
                    "event": "movers.snapshot", "payload": result,
                    "ts": result.get("updated_at") or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                })
                await self.cache.set_json(marker_key, version, ttl_seconds=3600)
            except Exception as exc:
                logger.warning("No se pudo publicar snapshot de movers (%s): %s", name, type(exc).__name__)

    async def run_once(self) -> dict[str, int]:
        await self._refresh_global_alert_snapshots()
        now_epoch = float(self.clock())
        now = datetime.fromtimestamp(now_epoch, tz=timezone.utc)
        self._telegram_sent = 0
        users = await asyncio.to_thread(self.auth.list_users_for_alerts, limit=5000)
        created = 0
        price_triggered = 0
        market_candidates = 0
        telegram_queued = 0
        quotes = await self.cache.get_json(QUOTES_KEY)
        quote_map = quotes if isinstance(quotes, dict) else {}

        # Personal price alerts are one-shot by default; repeat alerts obey their cooldown.
        for alert in await asyncio.to_thread(self.features.list_enabled_price_alerts, limit=5000):
            symbol = str(alert.get("symbol") or "").upper()
            quote = quote_map.get(symbol) if isinstance(quote_map, dict) else None
            price = _num(quote.get("price")) if isinstance(quote, dict) else None
            target = _num(alert.get("target_price"))
            if price is None or target is None or price <= 0:
                continue
            condition = str(alert.get("condition") or "")
            triggered = price >= target if condition == "above" else price <= target if condition == "below" else False
            if not triggered:
                continue
            user_id = str(alert.get("user_id") or "")
            user = await asyncio.to_thread(self.auth.get_user_by_id, user_id)
            if not user or not self._user_allowed(user, now):
                continue
            prefs = alert_preferences(user)
            if not (prefs.get("on") and prefs.get("price") and (prefs.get("in_app") or prefs.get("telegram") or prefs.get("web_push"))):
                continue
            claimed = await asyncio.to_thread(self.features.claim_price_alert_trigger, str(alert.get("id")), now=now)
            if not claimed:
                continue
            price_triggered += 1
            target_text = f"{target:,.8g}"
            current_text = f"{price:,.8g}"
            direction_text = "alcanzó o superó" if condition == "above" else "alcanzó o cayó por debajo de"
            message = f"{symbol}: el precio actual ({current_text}) {direction_text} tu nivel de alerta ({target_text})."
            event_id = f"price-alert:{alert.get('id')}:trigger:{int(claimed.get('trigger_count', 1))}" if alert.get("repeat") else f"price-alert:{alert.get('id')}:triggered"
            made, queued_telegram = await self._notify_user(
                user, event_id=event_id, kind="price", title=f"Alerta de precio · {symbol}", message=message,
                symbol=symbol, metadata={"alert_id": alert.get("id"), "condition": condition,
                                         "target_price": target, "observed_price": price}, now=now,
            )
            created += int(made)
            telegram_queued += int(queued_telegram)

        # The detector ports the bot's thresholds: complete windows, exact volume
        # confirmation, persisted per-key cooldown/escalation and long/short semantics.
        candidates = await self._global_market_candidates(now_epoch)
        candidates = await self._filter_global_market_candidates(candidates, now_epoch)
        market_candidates = len(candidates)
        for candidate in candidates:
            for user in users:
                if not self._user_allowed(user, now) or not wants_alert(user, candidate["kind"]):
                    continue
                prefs = alert_preferences(user)
                if not (prefs.get("in_app") or prefs.get("telegram") or prefs.get("web_push")):
                    continue
                made, queued_telegram = await self._notify_user(
                    user, event_id=candidate["event_id"], kind=candidate["kind"],
                    title=candidate["title"], message=candidate["message"], symbol=candidate.get("symbol"),
                    metadata=candidate.get("metadata") or {}, now=now,
                )
                created += int(made)
                telegram_queued += int(queued_telegram)

        status = {"status": "ok", "checked_at": now.isoformat().replace("+00:00", "Z"),
                  "users_scanned": len(users), "price_alerts_triggered": price_triggered,
                  "market_candidates": market_candidates, "notifications_created": created,
                  "telegram_queued": telegram_queued, "telegram_sent": 0}
        try:
            await self.cache.set_json(DISPATCH_STATUS_KEY, status, ttl_seconds=3600)
        except Exception:
            logger.exception("No se pudo guardar el estado del dispatcher de alertas")
        return status

    def _user_allowed(self, user: Mapping[str, Any], now: datetime) -> bool:
        user_id = str(user.get("user_id") or "")
        return bool(user_id and evaluate_access(user, now=now, is_admin=is_admin_user(user))["allowed"])

    async def _notify_user(self, user: Mapping[str, Any], *, event_id: str, kind: str, title: str,
                           message: str, symbol: str | None, metadata: dict[str, Any], now: datetime) -> tuple[bool, bool]:
        user_id = str(user.get("user_id") or "")
        prefs = alert_preferences(user)
        use_in_app = bool(prefs.get("in_app", True))
        chat_id = user.get("telegram_chat_id")
        # Telegram is deliberately not sent inline. The outbox fields are persisted
        # together with the notification so a crash cannot lose a delivery request.
        use_telegram = bool(prefs.get("telegram", True) and chat_id)
        try:
            use_web_push = bool(prefs.get("web_push", False)
                                and self.features.active_push_subscription_count(user_id) > 0)
        except Exception:
            logger.warning("No se pudo comprobar las suscripciones push del usuario %s", user_id, exc_info=True)
            use_web_push = False
        if not user_id or not (use_in_app or use_telegram or use_web_push):
            return False, False
        doc = {
            "id": uuid.uuid4().hex, "user_id": user_id, "event_id": str(event_id), "kind": str(kind),
            "title": str(title)[:160], "message": str(message)[:2000], "symbol": symbol,
            "metadata": dict(metadata), "created_at": now, "read_at": None, "in_app": use_in_app,
        }
        if use_telegram:
            doc["telegram_delivery"] = {
                "status": "pending", "chat_id": str(chat_id), "text": f"{str(title)[:160]}\n\n{str(message)[:2000]}",
                "attempts": 0, "next_attempt_at": now, "created_at": now, "updated_at": now,
            }
        if use_web_push:
            doc["web_push_delivery"] = {
                "status": "pending", "attempts": 0, "endpoint_results": {},
                "next_attempt_at": now, "created_at": now, "updated_at": now,
            }
        try:
            saved, created = await asyncio.to_thread(self.features.create_notification, doc)
        except Exception:
            logger.exception("No se pudo persistir una notificación para el usuario %s", user_id)
            return False, False
        if not created:
            return False, False
        if use_in_app:
            try:
                await self.cache.publish_json(NOTIFICATION_CHANNEL, {
                    "event": "notification.created", "user_id": user_id,
                    "notification": {"id": saved.get("id"), "event_id": saved.get("event_id"), "kind": kind,
                                     "title": title, "message": message, "symbol": symbol,
                                     "created_at": now.isoformat().replace("+00:00", "Z")},
                })
            except Exception:
                logger.warning("La notificación quedó guardada pero no se pudo publicar en Redis", exc_info=True)
        # Second value now means queued, not sent. Actual delivery and retries are
        # handled by worker.telegram_notification_delivery.
        return True, use_telegram

    async def _filter_global_market_candidates(self, candidates: list[dict[str, Any]], now_epoch: float) -> list[dict[str, Any]]:
        """Port the bot's per-alert cooldown, escalation exception and hourly cap.

        State is persisted in Redis so worker restarts do not reset market-alert cooldowns.
        This worker is intended to run as a single active dispatcher; a future multi-worker
        deployment must replace this read/modify/write with an atomic Redis lease/script.
        """
        if not candidates:
            return []
        raw_state = await self.cache.get_json(GLOBAL_ALERT_STATE_KEY)
        state = raw_state if isinstance(raw_state, dict) else {}
        last_sent = state.get("last_sent") if isinstance(state.get("last_sent"), dict) else {}
        sent_epochs = state.get("sent_epochs") if isinstance(state.get("sent_epochs"), list) else []
        sent_recent = []
        for value in sent_epochs:
            timestamp = _num(value)
            if timestamp is not None and 0 <= now_epoch - timestamp <= 3600:
                sent_recent.append(timestamp)

        best: dict[str, dict[str, Any]] = {}
        for candidate in candidates:
            key = str(candidate.get("alert_key") or "")
            if not key:
                continue
            prior = best.get(key)
            magnitude = _num(candidate.get("magnitude")) or 0.0
            prior_magnitude = _num((prior or {}).get("magnitude")) or 0.0
            if prior is None or magnitude > prior_magnitude:
                best[key] = candidate

        accepted: list[dict[str, Any]] = []
        priority = {"liq:market": 0}
        cooldown = max(1.0, _env_float("ALERTS_COOLDOWN_MINUTES", 45.0)) * 60.0
        market_cooldown = max(1.0, _env_float("ALERTS_MARKET_COOLDOWN_MINUTES", 20.0)) * 60.0
        max_per_hour = max(1, int(_env_float("ALERTS_MAX_PER_HOUR", 8)))
        for key, candidate in sorted(best.items(), key=lambda pair: (priority.get(pair[0], 1), -(_num(pair[1].get("magnitude")) or 0.0))):
            if len(sent_recent) >= max_per_hour:
                break
            previous = last_sent.get(key)
            can_send = True
            if isinstance(previous, dict):
                previous_at = _num(previous.get("timestamp"))
                previous_magnitude = _num(previous.get("magnitude")) or 0.0
                if previous_at is not None:
                    elapsed = now_epoch - previous_at
                    allowed_cooldown = market_cooldown if key == "liq:market" else cooldown
                    can_send = elapsed >= allowed_cooldown or (elapsed >= 300 and (_num(candidate.get("magnitude")) or 0.0) >= previous_magnitude * 2)
            if not can_send:
                continue
            candidate = dict(candidate)
            candidate["event_id"] = f"market-alert:{key}:{int(now_epoch)}"
            accepted.append(candidate)
            last_sent[key] = {"timestamp": now_epoch, "magnitude": _num(candidate.get("magnitude")) or 0.0}
            sent_recent.append(now_epoch)

        # Bound the state size and persist the policy across process restarts.
        last_sent = {key: value for key, value in last_sent.items()
                     if isinstance(value, dict) and (_num(value.get("timestamp")) or 0) >= now_epoch - 7200}
        await self.cache.set_json(GLOBAL_ALERT_STATE_KEY, {"last_sent": last_sent, "sent_epochs": sent_recent}, ttl_seconds=7200)
        return accepted

    async def _global_market_candidates(self, now_epoch: float) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        liq = await self.cache.get_json("market:liquidations:summary:v1:5")
        if isinstance(liq, dict) and liq.get("coverage_complete") and liq.get("status") == "ok":
            total = _num(liq.get("total")) or 0.0
            share = _num(liq.get("long_share"))
            market_min = _env_float("ALERT_LIQ_MARKET_USD", 12_000_000)
            market_share = _env_float("ALERT_LIQ_MARKET_SHARE", 0.70)
            if share is not None and total >= market_min and max(share, 1 - share) >= market_share:
                dominant = "longs" if share >= 0.5 else "shorts"
                top = liq.get("top") if isinstance(liq.get("top"), list) else []
                top_text = ", ".join(f"{str(row.get('symbol','')).removesuffix('USDT')} ${(_num(row.get('usd')) or 0)/1_000_000:.2f}M" for row in top[:3] if isinstance(row, dict))
                candidates.append({"kind": "liq", "alert_key": "liq:market", "magnitude": total,
                                   "event_id": "market:liq:market", "title": "Barrido de liquidaciones del mercado",
                                   "message": f"Se detectaron al menos ${total:,.0f} en liquidaciones en 5 minutos. Predominan los {dominant} ({max(share, 1-share)*100:.0f}%). Mayores pares: {top_text or 'sin detalle'}.",
                                   "symbol": (top[0].get("symbol") if top and isinstance(top[0], dict) else None),
                                   "metadata": {"window": 5, "total_usd": total, "long_share": share, "top": top[:5]}})
            else:
                symbol_min = _env_float("ALERT_LIQ_SYMBOL_USD", 3_000_000)
                symbol_share = _env_float("ALERT_LIQ_SYMBOL_SHARE", 0.75)
                for row in (liq.get("top") or [])[:5]:
                    if not isinstance(row, dict):
                        continue
                    usd = _num(row.get("usd")) or 0.0
                    long_usd = _num(row.get("long")) or 0.0
                    short_usd = _num(row.get("short")) or 0.0
                    symbol = str(row.get("symbol") or "").upper()
                    if usd < symbol_min:
                        break
                    if symbol and usd > 0 and max(long_usd, short_usd) / usd >= symbol_share:
                        side = "longs" if long_usd >= short_usd else "shorts"
                        candidates.append({"kind": "liq", "alert_key": f"liq:{symbol}", "magnitude": usd,
                                           "event_id": f"market:liq:{symbol}", "title": f"Liquidaciones concentradas · {symbol}",
                                           "message": f"Se detectaron al menos ${usd:,.0f} liquidados en {symbol} en 5 minutos; predominan los {side}.",
                                           "symbol": symbol, "metadata": {"window": 5, "total_usd": usd, "long_usd": long_usd, "short_usd": short_usd}})

        move_rules = ((5, _env_float("ALERT_MOVE_PCT_5M", 3.5), _env_float("ALERT_MAJOR_MOVE_PCT_5M", 1.0)),
                      (15, _env_float("ALERT_MOVE_PCT_15M", 6.0), _env_float("ALERT_MAJOR_MOVE_PCT_15M", 1.8)))
        for window, alt_threshold, major_threshold in move_rules:
            snap = await self.cache.get_json(f"market:movers:computed:v1:{window}")
            if not isinstance(snap, dict) or not snap.get("ready"):
                continue
            for group in (snap.get("gainers") or [], snap.get("losers") or []):
                for row in group:
                    if not isinstance(row, dict):
                        continue
                    symbol = str(row.get("symbol") or "").upper()
                    change = _num(row.get("change_pct"))
                    ratio = _num(row.get("volume_ratio"))
                    exact = row.get("volume_exact") is True
                    threshold = major_threshold if symbol in MAJORS else alt_threshold
                    if not symbol or change is None or abs(change) < threshold or ratio is None or ratio < _env_float("ALERT_MOVE_VOLUME", 3.0) or not exact:
                        continue
                    direction = "up" if change > 0 else "down"
                    side = "PUMP" if change > 0 else "DUMP"
                    candidates.append({"kind": "mov", "alert_key": f"mov:{symbol}:{direction}", "magnitude": abs(change),
                                       "event_id": f"market:mov:{symbol}:{direction}",
                                       "title": f"Aviso {side} · {symbol}",
                                       "message": f"{symbol} se movió {change:+.2f}% en {window} min con volumen x{ratio:.1f} de lo normal. Este aviso describe una anomalía de mercado, no una señal de entrada.",
                                       "symbol": symbol, "metadata": {"window": window, "change_pct": change, "volume_ratio": ratio,
                                                                         "buy_pct": _num(row.get("buy_pct")), "price": _num(row.get("price"))}})
        snap = await self.cache.get_json("market:movers:computed:v1:15")
        if isinstance(snap, dict) and snap.get("ready"):
            for row in snap.get("flat_volume") or []:
                if not isinstance(row, dict) or row.get("volume_exact") is not True:
                    continue
                symbol = str(row.get("symbol") or "").upper()
                ratio = _num(row.get("volume_ratio")) or 0.0
                change = _num(row.get("change_pct"))
                if symbol and ratio >= _env_float("ALERT_VOLUME_RATIO", 6.0) and change is not None and abs(change) < 1.0:
                    candidates.append({"kind": "vol", "alert_key": f"vol:{symbol}", "magnitude": ratio,
                                       "event_id": f"market:vol:{symbol}", "title": f"Volumen anormal · {symbol}",
                                       "message": f"{symbol} muestra volumen x{ratio:.1f} de lo normal con un movimiento de solo {change:+.2f}% en 15 minutos.",
                                       "symbol": symbol, "metadata": {"window": 15, "change_pct": change, "volume_ratio": ratio,
                                                                         "buy_pct": _num(row.get("buy_pct"))}})
        # Keep the strongest candidate when the same pair qualifies in 5m and 15m.
        deduped: dict[str, dict[str, Any]] = {}
        for item in candidates:
            # For movers the stable identity omits the observation window; keep largest move.
            if item["kind"] == "mov":
                meta = item.get("metadata") or {}
                base_key = f"mov:{item.get('symbol')}:{'up' if (_num(meta.get('change_pct')) or 0)>0 else 'down'}"
                prior = deduped.get(base_key)
                if prior is None or abs(_num(meta.get("change_pct")) or 0) > abs(_num((prior.get("metadata") or {}).get("change_pct")) or 0):
                    deduped[base_key] = item
            else:
                deduped[str(item["event_id"])] = item
        return list(deduped.values())


async def run_user_alerts_loop() -> None:
    from api.repositories.signals import repository_from_environment
    from api.repositories.sweeps import sweep_store_from_environment
    from api.services.market_intelligence import MarketIntelligenceService
    from worker.binance_rest import BinanceRestClient

    auth_repository = auth_repository_from_environment()
    feature_repository = user_feature_repository_from_environment()
    cache = cache_from_environment()
    signal_repository = repository_from_environment()
    sweep_store = sweep_store_from_environment()
    rest = BinanceRestClient()
    telegram = TelegramBotAPI()
    intelligence = MarketIntelligenceService(cache, rest, signal_repository, sweep_store)
    processor = UserAlertProcessor(auth_repository, feature_repository, cache, telegram, market_intelligence=intelligence)
    interval = max(5.0, float(os.getenv("USER_ALERTS_INTERVAL_SECONDS", "10")))
    try:
        from worker.distributed_lease import DistributedWorkerLease
        async with DistributedWorkerLease(cache, "user-alert-dispatcher"):
            while True:
                started = time.monotonic()
                try:
                    await processor.run_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Ciclo de alertas de usuario falló")
                await asyncio.sleep(max(0.1, interval - (time.monotonic() - started)))
    finally:
        feature_repository.close()
        auth_repository.close()
        signal_closer = getattr(signal_repository, "close", None)
        if callable(signal_closer):
            signal_closer()
        sweep_closer = getattr(sweep_store, "close", None)
        if callable(sweep_closer):
            sweep_closer()
        await rest.close()
        await cache.close()


def main() -> None:
    import logging
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    asyncio.run(run_user_alerts_loop())


if __name__ == "__main__":
    main()
