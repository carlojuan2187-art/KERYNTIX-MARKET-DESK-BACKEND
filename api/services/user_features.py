"""Business services for watchlists, price alerts and the notification inbox."""
from __future__ import annotations

import asyncio
import logging
import math
import uuid
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Mapping

from engine._lazy import LazyModule

pd = LazyModule("pandas")  # deferred: only the watchlist analysis path needs pandas

from fastapi import HTTPException

from api.repositories.auth import AuthRepository
from api.repositories.signals import SignalRepository, RepositoryUnavailable
from api.repositories.user_features import UserFeatureRepository
from api.services.market import QUOTES_KEY, MarketDataService
from api.cache import JsonCache
from engine.watchlist import (MAX_PAIRS, TIMEFRAMES, normalize_symbol, short_name, user_symbols, compute_score)


def analyze_timeframe(*args: Any, **kwargs: Any) -> Any:
    """Lazy proxy so importing this module does not import pandas/numpy via engine.regime."""
    from engine.regime import analyze_timeframe as _analyze_timeframe
    return _analyze_timeframe(*args, **kwargs)


from api.repositories.sweeps import SweepStore
from worker.binance_rest import BinanceDataError

logger = logging.getLogger("ftrade.user_features")
UTC = timezone.utc
DEFAULT_ALERT_PREFS: dict[str, bool] = {
    # Matches the old bot's default: alerts are on until the user changes them.
    "on": True, "liq": True, "mov": True, "vol": True,
    "signals": True, "price": True, "plans": True,
    "in_app": True, "telegram": True, "web_push": False,
}
ALERT_PREF_KEYS = frozenset(DEFAULT_ALERT_PREFS)


def iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        moment = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        return moment.isoformat().replace("+00:00", "Z")
    if isinstance(value, str):
        return value
    return None


def _safe_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def alert_preferences(user: Mapping[str, Any] | None) -> dict[str, bool]:
    stored = (user or {}).get("alert_prefs")
    result = dict(DEFAULT_ALERT_PREFS)
    if isinstance(stored, Mapping):
        for key in ALERT_PREF_KEYS:
            if key in stored:
                result[key] = bool(stored[key])
    return result


def wants_alert(user: Mapping[str, Any] | None, kind: str) -> bool:
    prefs = alert_preferences(user)
    return bool(prefs.get("on") and prefs.get(kind))


def _http(code: str, status: int, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


class WatchlistService:
    ANALYSIS_TTL_SECONDS = 60
    SWEEP_TTL_SECONDS = 10
    SWEEP_WINDOW_MINUTES = 20
    SWEEP_PROFILE = "plus"

    def __init__(self, auth_repository: AuthRepository, market_service: MarketDataService,
                 cache: JsonCache, signal_repository: SignalRepository,
                 sweep_store: SweepStore | None = None, *, clock=time.time) -> None:
        self.auth = auth_repository
        self.market = market_service
        self.cache = cache
        self.signals = signal_repository
        self.sweep_store = sweep_store
        self.clock = clock
        self._analysis_semaphore = asyncio.Semaphore(4)
        self._symbol_locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def _frame_from_klines(rows: list[list[Any]]) -> pd.DataFrame:
        columns = ["open_time", "open", "high", "low", "close", "volume", "close_time",
                   "quote_volume", "trades", "taker_buy_base", "taker_buy_quote", "ignore"]
        if not rows:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "close_time"])
        frame = pd.DataFrame(rows, columns=columns[:len(rows[0])])
        required = {"open", "high", "low", "close", "volume", "close_time"}
        if not required.issubset(frame.columns):
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "close_time"])
        frame["close_time"] = pd.to_datetime(frame["close_time"], unit="ms", utc=True, errors="coerce")
        for column in ("open", "high", "low", "close", "volume"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        return frame[["open", "high", "low", "close", "volume", "close_time"]].dropna().reset_index(drop=True)

    async def _symbol_analysis(self, symbol: str) -> dict[str, Any]:
        cache_key = f"market:watchlist-analysis:v1:{symbol}"
        cached = await self.cache.get_json(cache_key)
        if isinstance(cached, dict) and any(cached.get(tf) for tf in TIMEFRAMES):
            return cached
        lock = self._symbol_locks.setdefault(symbol, asyncio.Lock())
        async with lock:
            cached = await self.cache.get_json(cache_key)
            if isinstance(cached, dict) and any(cached.get(tf) for tf in TIMEFRAMES):
                return cached
            rest = getattr(self.market, "rest", None)
            if rest is None or not hasattr(rest, "fetch_klines"):
                return {tf: None for tf in TIMEFRAMES}
            async with self._analysis_semaphore:
                fetched = await asyncio.gather(
                    *(rest.fetch_klines(symbol, tf, 240) for tf in TIMEFRAMES),
                    return_exceptions=True,
                )
            frames = {tf: fetched[i] if not isinstance(fetched[i], Exception) else []
                      for i, tf in enumerate(TIMEFRAMES)}
            def analyze() -> dict[str, Any]:
                result: dict[str, Any] = {}
                for tf, rows in frames.items():
                    try:
                        result[tf] = analyze_timeframe(self._frame_from_klines(rows))
                    except Exception:
                        result[tf] = None
                return result
            analyses = await asyncio.to_thread(analyze)
            if any(analyses.values()):
                await self.cache.set_json(cache_key, analyses, ttl_seconds=self.ANALYSIS_TTL_SECONDS)
            return analyses

    @staticmethod
    def _parse_sweep_epoch(value: Any) -> float | None:
        if isinstance(value, datetime):
            dt = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
            return dt.timestamp()
        if isinstance(value, str):
            try:
                dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
                dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
                return dt.timestamp()
            except ValueError:
                return None
        return None

    async def _recent_sweeps(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        now = float(self.clock())
        cache_key = "market:watchlist-sweeps:v1"
        rows = await self.cache.get_json(cache_key)
        if not isinstance(rows, list):
            rows = []
            if self.sweep_store is not None:
                try:
                    active = await asyncio.to_thread(
                        self.sweep_store.load_active, now=datetime.fromtimestamp(now, tz=UTC)
                    )
                    for key, raw in (active or {}).items():
                        if not isinstance(raw, Mapping):
                            continue
                        parts = str(key).split(":")
                        symbol = str(raw.get("symbol") or (parts[0] if parts else "")).upper()
                        profile = str(raw.get("filter_profile") or raw.get("profile") or (parts[1] if len(parts) > 2 else "")).lower()
                        direction = str(raw.get("direction") or (parts[2] if len(parts) > 2 else "")).upper()
                        event_epoch = self._parse_sweep_epoch(raw.get("event_at"))
                        if symbol and profile == self.SWEEP_PROFILE and direction in {"LONG", "SHORT"} and event_epoch is not None:
                            rows.append({"symbol": symbol, "direction": direction, "event_epoch": event_epoch})
                    await self.cache.set_json(cache_key, rows, ttl_seconds=self.SWEEP_TTL_SECONDS)
                except Exception:
                    logger.debug("Watchlist: barridos activos no disponibles", exc_info=True)
        result: dict[str, dict[str, Any]] = {}
        wanted = set(symbols)
        for item in rows:
            if not isinstance(item, Mapping) or str(item.get("symbol") or "").upper() not in wanted:
                continue
            event_epoch = _safe_float(item.get("event_epoch"))
            direction = str(item.get("direction") or "").upper()
            if event_epoch is None or direction not in {"LONG", "SHORT"}:
                continue
            minutes = (now - event_epoch) / 60.0
            if minutes < -1 or minutes >= self.SWEEP_WINDOW_MINUTES:
                continue
            symbol = str(item.get("symbol")).upper()
            prior = result.get(symbol)
            if prior is None or minutes < prior["minutes"]:
                result[symbol] = {"direction": direction, "minutes": max(0.0, minutes)}
        return result

    async def _quote_map(self, *, require_live: bool = False) -> tuple[dict[str, dict[str, Any]], str, str | None]:
        try:
            rows, feed = await self.market.quotes()
            mapped = {}
            for item in rows:
                if hasattr(item, "model_dump"):
                    row = item.model_dump(mode="json")
                elif isinstance(item, Mapping):
                    row = dict(item)
                else:
                    continue
                symbol = str(row.get("symbol") or "").upper()
                if symbol:
                    mapped[symbol] = row
            if mapped:
                return mapped, str(getattr(feed, "status", "ok")), iso(getattr(feed, "updated_at", None))
        except Exception as exc:
            logger.warning("Watchlist: cotizaciones no disponibles: %s", exc)
        cached = await self.cache.get_json(QUOTES_KEY)
        if isinstance(cached, dict) and cached:
            mapped = {str(sym).upper(): dict(row) for sym, row in cached.items() if isinstance(row, dict)}
            if mapped:
                return mapped, "degraded", None
        if require_live:
            raise _http("MARKET_UNAVAILABLE", 503, "No se pudo verificar el mercado de futuros en este momento.")
        return {}, "unavailable", None

    def _user(self, user_id: str) -> dict[str, Any]:
        user = self.auth.get_user_by_id(str(user_id))
        if not user:
            raise _http("USER_NOT_FOUND", 404, "La cuenta no existe.")
        return user

    async def list(self, user_id: str) -> dict[str, Any]:
        user = self._user(user_id)
        symbols = user_symbols(user)
        quotes, status, updated_at = await self._quote_map()
        try:
            signals = await asyncio.to_thread(self.signals.list_signals, limit=300)
        except Exception:
            signals = []
        active: dict[str, dict[str, Any]] = {}
        for signal in signals:
            symbol = str(signal.get("symbol") or "").upper()
            state = str(signal.get("status") or ((signal.get("lifecycle") or {}).get("status") if isinstance(signal.get("lifecycle"), dict) else "") or "").upper()
            if symbol in symbols and state in {"PENDING", "TOUCHED"} and symbol not in active:
                raw_entry_zone = signal.get("entry_zone") if isinstance(signal.get("entry_zone"), Mapping) else {}
                raw_targets = signal.get("take_profits")
                if not isinstance(raw_targets, list) or not raw_targets:
                    raw_targets = [signal.get("tp1"), signal.get("tp2"), signal.get("tp3")]
                take_profits = [value for value in (_safe_float(target) for target in raw_targets) if value is not None and value > 0]
                active[symbol] = {
                    "id": str(signal.get("id") or signal.get("_id") or ""),
                    "direction": signal.get("direction"), "status": state,
                    "score": signal.get("score"), "entry_price": _safe_float(signal.get("entry_price")),
                    "entry_low": _safe_float(signal.get("entry_low")) if _safe_float(signal.get("entry_low")) is not None else _safe_float(raw_entry_zone.get("low")),
                    "entry_high": _safe_float(signal.get("entry_high")) if _safe_float(signal.get("entry_high")) is not None else _safe_float(raw_entry_zone.get("high")),
                    "stop_loss": _safe_float(signal.get("stop_loss")),
                    "take_profits": take_profits,
                    "created_at": iso(signal.get("created_at")),
                }
        # Strategy-aligned trend/score enrichment. Each symbol is analyzed once per shared
        # cache TTL rather than once per user, matching the original Telegram watchlist.
        analyses_by_symbol = await asyncio.gather(*(self._symbol_analysis(symbol) for symbol in symbols)) if symbols else []
        analyses_map = dict(zip(symbols, analyses_by_symbol))
        sweeps = await self._recent_sweeps(symbols) if symbols else {}
        items = []
        for symbol in symbols:
            quote = quotes.get(symbol)
            analyses = analyses_map.get(symbol) or {tf: None for tf in TIMEFRAMES}
            sweep = sweeps.get(symbol)
            score_detail = compute_score(analyses, sweep)
            trend = {
                "15M": (analyses.get("15m") or {}).get("regime") if isinstance(analyses.get("15m"), dict) else None,
                "1H": (analyses.get("1h") or {}).get("regime") if isinstance(analyses.get("1h"), dict) else None,
                "4H": (analyses.get("4h") or {}).get("regime") if isinstance(analyses.get("4h"), dict) else None,
            }
            if quote is None:
                items.append({"symbol": symbol, "name": short_name(symbol), "status": "unavailable",
                              "price": None, "change_pct": None, "volume_24h": None,
                              "funding_rate": None, "trend": trend, "analyses": analyses,
                              "score": score_detail.get("score") if score_detail else None,
                              "score_detail": score_detail, "liquidity_sweep": sweep,
                              "active_signal": active.get(symbol)})
                continue
            items.append({
                "symbol": symbol, "name": short_name(symbol), "status": "ok",
                "price": quote.get("price"), "change_pct": quote.get("change_pct"),
                "volume_24h": quote.get("volume_24h"), "funding_rate": quote.get("funding_rate"),
                "trend": trend, "analyses": analyses,
                "score": score_detail.get("score") if score_detail else None,
                "score_detail": score_detail,
                "liquidity_sweep": sweep,
                "active_signal": active.get(symbol),
                "data_updated_at": quote.get("updated_at") or updated_at,
            })
        return {"items": items, "total": len(items), "max_items": MAX_PAIRS,
                "status": status, "source": "user-watchlist+shared-market-cache", "updated_at": updated_at,
                "analysis_status": "ready" if any(any(value for value in analysis.values()) for analysis in analyses_by_symbol) else "collecting"}

    async def add(self, user_id: str, raw_symbol: str) -> dict[str, Any]:
        try:
            symbol = normalize_symbol(raw_symbol)
        except ValueError as exc:
            raise _http("INVALID_SYMBOL", 422, "El símbolo no tiene el formato de un perpetuo USDT válido.") from exc
        quotes, _status, _updated = await self._quote_map(require_live=True)
        if symbol not in quotes:
            raise _http("SYMBOL_NOT_AVAILABLE", 422, "El símbolo no aparece en el mercado de futuros disponible.")
        user, outcome = self.auth.add_watchlist_symbol(str(user_id), symbol, max_items=MAX_PAIRS)
        if outcome == "USER_NOT_FOUND" or user is None:
            raise _http("USER_NOT_FOUND", 404, "La cuenta no existe.")
        if outcome == "DUPLICATE":
            raise _http("WATCHLIST_DUPLICATE", 409, "Ese par ya está en tu watchlist.")
        if outcome == "FULL":
            raise _http("WATCHLIST_FULL", 409, f"La watchlist admite un máximo de {MAX_PAIRS} pares.")
        if outcome == "CONFLICT":
            raise _http("WATCHLIST_CONFLICT", 409, "La watchlist cambió durante la operación. Vuelve a intentarlo.")
        return await self.list(user_id)

    async def remove(self, user_id: str, raw_symbol: str) -> dict[str, Any]:
        try:
            symbol = normalize_symbol(raw_symbol)
        except ValueError as exc:
            raise _http("INVALID_SYMBOL", 422, "El símbolo no es válido.") from exc
        user, removed = self.auth.remove_watchlist_symbol(str(user_id), symbol)
        if user is None:
            raise _http("USER_NOT_FOUND", 404, "La cuenta no existe.")
        if not removed:
            raise _http("WATCHLIST_SYMBOL_NOT_FOUND", 404, "Ese par no está en tu watchlist.")
        return await self.list(user_id)


class AlertPreferencesService:
    def __init__(self, auth_repository: AuthRepository) -> None:
        self.auth = auth_repository

    def get(self, user_id: str) -> dict[str, Any]:
        user = self.auth.get_user_by_id(str(user_id))
        if not user:
            raise _http("USER_NOT_FOUND", 404, "La cuenta no existe.")
        return {"preferences": alert_preferences(user), "updated_at": iso(user.get("alert_prefs_updated_at"))}

    def patch(self, user_id: str, fields: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {str(key): bool(value) for key, value in fields.items() if key in ALERT_PREF_KEYS and value is not None}
        if not allowed:
            raise _http("EMPTY_PREFERENCES", 422, "Debes indicar al menos una preferencia.")
        user = self.auth.update_alert_preferences(str(user_id), allowed)
        if not user:
            raise _http("USER_NOT_FOUND", 404, "La cuenta no existe.")
        return self.get(user_id)


class PriceAlertService:
    def __init__(self, auth_repository: AuthRepository, feature_repository: UserFeatureRepository,
                 market_service: MarketDataService, cache: JsonCache) -> None:
        self.auth = auth_repository
        self.features = feature_repository
        self.market = market_service
        self.cache = cache

    async def _validate_market_symbol(self, symbol: str) -> str:
        try:
            normalized = normalize_symbol(symbol)
        except ValueError as exc:
            raise _http("INVALID_SYMBOL", 422, "El símbolo no es un perpetuo USDT válido.") from exc
        try:
            rows, _status = await self.market.quotes()
            symbols = {str(row.symbol if hasattr(row, "symbol") else row.get("symbol", "")).upper() for row in rows}
        except Exception:
            cached = await self.cache.get_json(QUOTES_KEY)
            symbols = {str(key).upper() for key in cached} if isinstance(cached, dict) else set()
        if normalized not in symbols:
            raise _http("SYMBOL_NOT_AVAILABLE", 422, "El símbolo no está disponible en el mercado actual.")
        return normalized

    def _user(self, user_id: str) -> dict[str, Any]:
        user = self.auth.get_user_by_id(str(user_id))
        if not user:
            raise _http("USER_NOT_FOUND", 404, "La cuenta no existe.")
        return user

    async def list(self, user_id: str, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        self._user(user_id)
        return self.features.list_price_alerts(str(user_id), enabled_only=enabled_only, limit=100)

    async def create(self, user_id: str, data: Mapping[str, Any]) -> dict[str, Any]:
        self._user(user_id)
        symbol = await self._validate_market_symbol(str(data["symbol"]))
        now = datetime.now(UTC)
        doc = {
            "id": uuid.uuid4().hex, "user_id": str(user_id), "symbol": symbol,
            "condition": str(data["condition"]), "target_price": float(data["target_price"]),
            "enabled": True, "repeat": bool(data.get("repeat", False)),
            "cooldown_seconds": max(60, min(86400, int(data.get("cooldown_seconds", 300)))),
            "created_at": now, "updated_at": now, "trigger_count": 0,
        }
        try:
            return self.features.create_price_alert(doc)
        except ValueError as exc:
            if str(exc) == "DUPLICATE_ACTIVE_PRICE_ALERT":
                raise _http("PRICE_ALERT_DUPLICATE", 409, "Ya existe una alerta activa para ese nivel y condición.") from exc
            raise

    def patch(self, user_id: str, alert_id: str, fields: Mapping[str, Any]) -> dict[str, Any]:
        self._user(user_id)
        update = {key: value for key, value in fields.items() if value is not None}
        if update.get("enabled") is True:
            update["reset_triggered"] = True
        if not update:
            raise _http("EMPTY_ALERT_PATCH", 422, "Debes indicar al menos un campo que modificar.")
        try:
            result = self.features.update_price_alert(str(alert_id), str(user_id), update)
        except Exception as exc:
            # MongoDB enforces the same invariant through its partial unique index;
            # map DuplicateKeyError to the same public API contract as the memory adapter.
            if str(exc) == "DUPLICATE_ACTIVE_PRICE_ALERT" or type(exc).__name__ == "DuplicateKeyError":
                raise _http("PRICE_ALERT_DUPLICATE", 409,
                            "Ya existe una alerta activa para ese nivel y condición.") from exc
            raise
        if not result:
            raise _http("PRICE_ALERT_NOT_FOUND", 404, "La alerta no existe.")
        return result

    def delete(self, user_id: str, alert_id: str) -> None:
        self._user(user_id)
        if not self.features.delete_price_alert(str(alert_id), str(user_id)):
            raise _http("PRICE_ALERT_NOT_FOUND", 404, "La alerta no existe.")


class NotificationService:
    def __init__(self, feature_repository: UserFeatureRepository) -> None:
        self.features = feature_repository

    def list(self, user_id: str, *, unread_only: bool = False, limit: int = 50, before: datetime | None = None) -> dict[str, Any]:
        rows = self.features.list_notifications(str(user_id), unread_only=unread_only, limit=limit, before=before)
        items = []
        for row in rows:
            items.append({
                "id": str(row.get("id", "")), "event_id": str(row.get("event_id", "")),
                "kind": str(row.get("kind", "general")), "title": str(row.get("title", "KERYNTIX MARKET DESK")),
                "message": str(row.get("message", "")), "symbol": row.get("symbol"),
                "metadata": row.get("metadata") if isinstance(row.get("metadata"), dict) else {},
                "created_at": iso(row.get("created_at")), "read_at": iso(row.get("read_at")),
            })
        next_before = items[-1]["created_at"] if len(items) == max(1, min(int(limit), 100)) else None
        return {"items": items, "unread_count": self.features.unread_count(str(user_id)),
                "limit": max(1, min(int(limit), 100)), "next_before": next_before}

    def mark_read(self, user_id: str, notification_id: str) -> dict[str, Any]:
        row = self.features.mark_notification_read(str(notification_id), str(user_id), now=datetime.now(UTC))
        if not row:
            raise _http("NOTIFICATION_NOT_FOUND", 404, "La notificación no existe.")
        return {"id": str(row.get("id", notification_id)), "read_at": iso(row.get("read_at")),
                "unread_count": self.features.unread_count(str(user_id))}

    def mark_all_read(self, user_id: str) -> dict[str, int]:
        count = self.features.mark_all_notifications_read(str(user_id), now=datetime.now(UTC))
        return {"updated": count, "unread_count": self.features.unread_count(str(user_id))}

    def delete(self, user_id: str, notification_id: str) -> None:
        if not self.features.delete_notification(str(notification_id), str(user_id)):
            raise _http("NOTIFICATION_NOT_FOUND", 404, "La notificación no existe.")
