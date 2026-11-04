"""Vendored, unmodified strategy logic from the current Telegram bot.

Source of truth: KERYNTIX MARKET DESK-main/app/strategy.py from the user-provided bot archive.
The scan service calls this module directly so web and bot calculations can be
compared without reimplementing the strategy rules in a second language.
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional, Tuple, Mapping

import os
import pandas as pd

class _BreakoutCompat:
    SEND_MODE_PENDING_ENTRY = "pending_entry"
    TRADING_PROFILES = {
        "conservador": {"leverage": "5x-10x"},
        "moderado": {"leverage": "10x-20x"},
        "agresivo": {"leverage": "30x-40x"},
    }

    @staticmethod
    def _env_float(name, default):
        try:
            return float(os.getenv(name, str(default)))
        except (TypeError, ValueError):
            return float(default)

    @staticmethod
    def _clamp(value, low, high):
        return max(float(low), min(float(high), float(value)))

    @staticmethod
    def _round_price_dynamic(value):
        value = float(value)
        if value >= 1000:
            return round(value, 2)
        if value >= 1:
            return round(value, 4)
        return round(value, 8)

breakout = _BreakoutCompat()

LIQUIDITY_LOOKBACK = max(24, int(os.getenv("LIQUIDITY_LOOKBACK", "36")))
LIQUIDITY_PIVOT_LEFT = max(1, int(os.getenv("LIQUIDITY_PIVOT_LEFT", "2")))
LIQUIDITY_PIVOT_RIGHT = max(1, int(os.getenv("LIQUIDITY_PIVOT_RIGHT", "2")))
SWEEP_SEARCH_BARS = max(2, int(os.getenv("LIQUIDITY_SWEEP_SEARCH_BARS", "4")))
MIN_HISTORY_BARS = max(LIQUIDITY_LOOKBACK + 4, 36)
SCORE_CALIBRATION_VERSION = "v4_5_plus_pullback_reentry_structural_tp2"
ENTRY_MODEL_NAME = "liquidity_sweep_reversal_predictive_pullback_v4"
SETUP_STAGE_WAITING_PULLBACK = "confirmed_waiting_pullback"
SEND_MODE_PENDING_ENTRY = breakout.SEND_MODE_PENDING_ENTRY

PREMIUM_RAW_SCORE_MIN = float(os.getenv("PREMIUM_RAW_SCORE_MIN", "83"))
PLUS_RAW_SCORE_MIN = float(os.getenv("PLUS_RAW_SCORE_MIN", "76"))
FREE_RAW_SCORE_MIN = float(os.getenv("FREE_RAW_SCORE_MIN", "69"))


FREE_PROFILE = {
    "name": "free",
    "filter_profile": "free",
    "atr_pct_min": breakout._env_float("LSR_FREE_ATR_PCT_MIN", 0.0012),
    "atr_pct_max": breakout._env_float("LSR_FREE_ATR_PCT_MAX", 0.0260),
    "liquidity_tolerance_atr": breakout._env_float("LSR_FREE_LIQUIDITY_TOLERANCE_ATR", 0.56),
    "min_sweep_atr": breakout._env_float("LSR_FREE_MIN_SWEEP_ATR", 0.07),
    "min_rel_volume": breakout._env_float("LSR_FREE_MIN_REL_VOLUME", 0.70),
    "min_confirm_rel_volume": breakout._env_float("LSR_FREE_MIN_CONFIRM_REL_VOLUME", 0.72),
    "min_body_ratio_recovery": breakout._env_float("LSR_FREE_MIN_BODY_RATIO_RECOVERY", 0.14),
    "min_body_ratio_confirmation": breakout._env_float("LSR_FREE_MIN_BODY_RATIO_CONFIRMATION", 0.12),
    "min_close_position": breakout._env_float("LSR_FREE_MIN_CLOSE_POSITION", 0.48),
    "min_rr": breakout._env_float("LSR_FREE_MIN_RR", 0.82),
    "htf_price_tolerance": breakout._env_float("LSR_FREE_HTF_PRICE_TOLERANCE", 0.0140),
    "htf_trend_tolerance": breakout._env_float("LSR_FREE_HTF_TREND_TOLERANCE", 0.0240),
    "htf_required_score": max(1, int(os.getenv("LSR_FREE_HTF_REQUIRED_SCORE", "1"))),
    "score": 78.0,
}

PLUS_PROFILE = {
    "name": "plus",
    "filter_profile": "plus",
    "atr_pct_min": breakout._env_float("LSR_PLUS_ATR_PCT_MIN", 0.0015),
    "atr_pct_max": breakout._env_float("LSR_PLUS_ATR_PCT_MAX", 0.0240),
    "liquidity_tolerance_atr": breakout._env_float("LSR_PLUS_LIQUIDITY_TOLERANCE_ATR", 0.50),
    "min_sweep_atr": breakout._env_float("LSR_PLUS_MIN_SWEEP_ATR", 0.10),
    "min_rel_volume": breakout._env_float("LSR_PLUS_MIN_REL_VOLUME", 0.76),
    "min_confirm_rel_volume": breakout._env_float("LSR_PLUS_MIN_CONFIRM_REL_VOLUME", 0.82),
    "min_body_ratio_recovery": breakout._env_float("LSR_PLUS_MIN_BODY_RATIO_RECOVERY", 0.17),
    "min_body_ratio_confirmation": breakout._env_float("LSR_PLUS_MIN_BODY_RATIO_CONFIRMATION", 0.15),
    "min_close_position": breakout._env_float("LSR_PLUS_MIN_CLOSE_POSITION", 0.53),
    "min_rr": breakout._env_float("LSR_PLUS_MIN_RR", 0.90),
    "htf_price_tolerance": breakout._env_float("LSR_PLUS_HTF_PRICE_TOLERANCE", 0.0110),
    "htf_trend_tolerance": breakout._env_float("LSR_PLUS_HTF_TREND_TOLERANCE", 0.0200),
    "htf_required_score": max(1, int(os.getenv("LSR_PLUS_HTF_REQUIRED_SCORE", "1"))),
    "score": 86.0,
}

PREMIUM_PROFILE = {
    "name": "premium",
    "filter_profile": "premium",
    "atr_pct_min": breakout._env_float("LSR_PREMIUM_ATR_PCT_MIN", 0.0018),
    "atr_pct_max": breakout._env_float("LSR_PREMIUM_ATR_PCT_MAX", 0.0225),
    "liquidity_tolerance_atr": breakout._env_float("LSR_PREMIUM_LIQUIDITY_TOLERANCE_ATR", 0.44),
    "min_sweep_atr": breakout._env_float("LSR_PREMIUM_MIN_SWEEP_ATR", 0.14),
    "min_rel_volume": breakout._env_float("LSR_PREMIUM_MIN_REL_VOLUME", 0.84),
    "min_confirm_rel_volume": breakout._env_float("LSR_PREMIUM_MIN_CONFIRM_REL_VOLUME", 0.90),
    "min_body_ratio_recovery": breakout._env_float("LSR_PREMIUM_MIN_BODY_RATIO_RECOVERY", 0.20),
    "min_body_ratio_confirmation": breakout._env_float("LSR_PREMIUM_MIN_BODY_RATIO_CONFIRMATION", 0.18),
    "min_close_position": breakout._env_float("LSR_PREMIUM_MIN_CLOSE_POSITION", 0.58),
    "min_rr": breakout._env_float("LSR_PREMIUM_MIN_RR", 0.98),
    "htf_price_tolerance": breakout._env_float("LSR_PREMIUM_HTF_PRICE_TOLERANCE", 0.0090),
    "htf_trend_tolerance": breakout._env_float("LSR_PREMIUM_HTF_TREND_TOLERANCE", 0.0160),
    # Endure un contexto de mayor respaldo en timeframes superiores.  El score
    # de contexto en 1H suma hasta cuatro criterios (precio sobre EMA20,
    # EMA20 sobre EMA50, momentum, y al menos un cierre reciente por encima
    # de EMA20).  Para la versión premium exigimos que se cumplan todos
    # (o casi todos) estos factores antes de permitir una señal; de lo contrario
    # la señal se descarta como de contexto débil.  Este valor debe ser
    # superior a la puntuación máxima esperable (4) para asegurar que sólo
    # contextos excepcionalmente alineados generen señales premium.
    "htf_required_score": max(1, int(os.getenv("LSR_PREMIUM_HTF_REQUIRED_SCORE", "5"))),
    "score": 90.0,
}


# MTF signal mapping:
# The single published KERYNTIX signal keeps its existing PREMIUM delivery tier,
# but its entry filters are calibrated to the user's reference PLUS profile.
# Only filter thresholds are borrowed; KERYNTIX retains its own 5m execution,
# structural/dynamic stop-loss and liquidity-based take-profit calculations.
# FREE trial users continue receiving this same published signal.
MTF_PREMIUM_PROFILE = dict(PLUS_PROFILE)
MTF_PREMIUM_PROFILE["name"] = "premium"
MTF_PREMIUM_PROFILE["filter_profile"] = "plus"
PROFILES = [MTF_PREMIUM_PROFILE]


LONG_DIRECTIONAL_TUNING = {
    "free": {
        "min_sweep_atr": 0.04,
        "min_confirm_rel_volume": 0.06,
        "min_body_ratio_confirmation": 0.04,
        "min_close_position": 0.04,
        "min_rr": 0.10,
        "htf_required_score": 0,
        "htf_price_tolerance_mul": 0.96,
        "htf_trend_tolerance_mul": 0.96,
    },
    "plus": {
        "min_sweep_atr": 0.05,
        "min_confirm_rel_volume": 0.07,
        "min_body_ratio_confirmation": 0.05,
        "min_close_position": 0.05,
        "min_rr": 0.12,
        "htf_required_score": 1,
        "htf_price_tolerance_mul": 0.92,
        "htf_trend_tolerance_mul": 0.92,
    },
    "premium": {
        "min_sweep_atr": 0.07,
        "min_confirm_rel_volume": 0.09,
        "min_body_ratio_confirmation": 0.05,
        "min_close_position": 0.05,
        "min_rr": 0.16,
        "htf_required_score": 1,
        "htf_price_tolerance_mul": 0.88,
        "htf_trend_tolerance_mul": 0.88,
    },
}

PREMIUM_GLOBAL_TUNING = {
    "min_rel_volume": 0.03,
    "min_confirm_rel_volume": 0.04,
    "min_body_ratio_confirmation": 0.02,
    "min_close_position": 0.02,
    "min_rr": 0.05,
}

LONG_SCORE_FLOOR_BONUS = {
    "free": 1.0,
    "plus": 2.0,
    "premium": 4.0,
}
PREMIUM_SCORE_FLOOR_BONUS = 2.0


# Ajuste moderado de ejecución: esperar un retroceso ligeramente más profundo mejora
# el precio de entrada potencial sin añadir filtros ni alterar el SL/TP estructural.
# Puede reducir algunos fills si el mercado no vuelve a la zona proyectada.
PULLBACK_RETRACE_FRACTION = breakout._env_float("LSR_PULLBACK_RETRACE_FRACTION", 0.42)
PULLBACK_ATR_MIN = breakout._env_float("LSR_PULLBACK_ATR_MIN", 0.24)
# Acorta el rango máximo de retroceso para evitar esperar pullbacks demasiado profundos
PULLBACK_ATR_MAX = breakout._env_float("LSR_PULLBACK_ATR_MAX", 0.70)
PULLBACK_LEVEL_BUFFER_ATR = breakout._env_float("LSR_PULLBACK_LEVEL_BUFFER_ATR", 0.04)
PULLBACK_MARKET_GAP_ATR = breakout._env_float("LSR_PULLBACK_MARKET_GAP_ATR", 0.03)
# Execution guard: prevents publishing setups after price has already run too far
# beyond the confirmation candle. 5m is used as the execution trigger while
# 15m remains the market-structure timeframe.
MAX_SIGNAL_EXTENSION_ATR = breakout._env_float("LSR_MAX_SIGNAL_EXTENSION_ATR", 0.35)
# La confirmación debe llegar pronto después del sweep. Si esperamos demasiadas
# velas 5m, la dirección puede seguir siendo correcta pero la entrada ya llega tarde.
MAX_BARS_AFTER_SWEEP = max(1, int(os.getenv("LSR_MAX_BARS_AFTER_SWEEP", "2")))
# Evita publicar cuando el precio ya recorrió demasiado desde el extremo del sweep.
MAX_SWEEP_TO_MARKET_ATR = breakout._env_float("LSR_MAX_SWEEP_TO_MARKET_ATR", 1.45)
# El primer cambio de estructura 5m debe aparecer en la confirmación.
REQUIRE_EARLY_STRUCTURE_SHIFT = os.getenv("LSR_REQUIRE_EARLY_STRUCTURE_SHIFT", "true").strip().lower() not in {"0", "false", "no", "off"}
# Control mínimo del primer objetivo. No fuerza un 1R rígido, para no destruir la
# frecuencia de la estrategia, pero descarta geometrías claramente pobres.
MIN_PUBLISH_TP1_RR = breakout._env_float("LSR_MIN_PUBLISH_TP1_RR", 0.80)
# Zona de entrada dinámica alrededor del pullback central.
ENTRY_ZONE_HALF_ATR = breakout._env_float("LSR_ENTRY_ZONE_HALF_ATR", 0.10)
ENTRY_ZONE_MIN_PCT = breakout._env_float("LSR_ENTRY_ZONE_MIN_PCT", 0.0006)
ENTRY_ZONE_MAX_PCT = breakout._env_float("LSR_ENTRY_ZONE_MAX_PCT", 0.0020)
MIN_PREENTRY_DISTANCE_ATR = breakout._env_float("LSR_MIN_PREENTRY_DISTANCE_ATR", 0.10)
MAX_PREENTRY_DISTANCE_ATR = breakout._env_float("LSR_MAX_PREENTRY_DISTANCE_ATR", 0.85)
EXECUTION_TIMEFRAME = "5M"
POST_FILL_INVALIDATION_MINUTES = max(5, int(os.getenv("LSR_POST_FILL_INVALIDATION_MINUTES", "35")))
POST_FILL_MIN_TP1_PROGRESS_PCT = breakout._env_float("LSR_POST_FILL_MIN_TP1_PROGRESS_PCT", 18.0)

# ---------------------------------------------------------------
# SL / TP ESTRUCTURALES (los calcula la estrategia, no porcentajes fijos)
# ---------------------------------------------------------------
# SL: justo detrás del extremo del barrido (si el precio lo pierde, la tesis de
#     "liquidez barrida" queda invalidada) más un colchón proporcional al ATR.
# TP: los siguientes pools de liquidez en la dirección del trade (máximos/mínimos
#     de swing de 15M y 1H, incluyendo "equal highs/lows" y el extremo del rango).
#     Se coloca ligeramente ANTES del nivel para que la orden se ejecute antes de
#     que el precio tome esa liquidez.
SL_BUFFER_ATR = breakout._env_float("LSR_SL_BUFFER_ATR", 0.12)
SL_MIN_RISK_ATR = breakout._env_float("LSR_SL_MIN_RISK_ATR", 0.30)   # suelo anti-ruido
SL_MAX_RISK_ATR = breakout._env_float("LSR_SL_MAX_RISK_ATR", 1.60)   # techo: stop demasiado lejos
TARGET_LOOKBACK_15M = max(24, int(os.getenv("LSR_TARGET_LOOKBACK_15M", "96")))
TARGET_LOOKBACK_1H = max(12, int(os.getenv("LSR_TARGET_LOOKBACK_1H", "48")))
# TP1 keeps its original lookback. TP2 may inspect a wider historical window,
# but it can only use confirmed historical swing/range levels (never a synthetic RR).
TARGET_TP2_LOOKBACK_MULTIPLIER = max(1.0, breakout._env_float("LSR_TARGET_TP2_LOOKBACK_MULTIPLIER", 2.0))
TARGET_PIVOT_BARS = max(1, int(os.getenv("LSR_TARGET_PIVOT_BARS", "2")))
TARGET_CLUSTER_ATR = breakout._env_float("LSR_TARGET_CLUSTER_ATR", 0.25)
TARGET_FRONTRUN_ATR = breakout._env_float("LSR_TARGET_FRONTRUN_ATR", 0.06)
TARGET_MIN_GAP_ATR = breakout._env_float("LSR_TARGET_MIN_GAP_ATR", 0.35)
TARGET_MAX_ATR = breakout._env_float("LSR_TARGET_MAX_ATR", 6.0)
TARGET_MODEL_NAME = "structural_liquidity_v2_extended_tp2"


def _record_reject(debug_counts: Optional[Dict[str, int]], reason: str) -> None:
    if debug_counts is None:
        return
    key = str(reason or "unknown").strip() or "unknown"
    debug_counts[key] = int(debug_counts.get(key, 0)) + 1




def _profile_name(profile: Dict) -> str:
    return str(profile.get("filter_profile") or profile.get("name") or "free").strip().lower() or "free"


def _directional_profile(profile: Dict, direction: str) -> Dict:
    tuned = dict(profile)
    profile_name = _profile_name(profile)

    if profile_name == "premium":
        tuned["min_rel_volume"] = float(tuned["min_rel_volume"]) + PREMIUM_GLOBAL_TUNING["min_rel_volume"]
        tuned["min_confirm_rel_volume"] = float(tuned["min_confirm_rel_volume"]) + PREMIUM_GLOBAL_TUNING["min_confirm_rel_volume"]
        tuned["min_body_ratio_confirmation"] = breakout._clamp(
            float(tuned["min_body_ratio_confirmation"]) + PREMIUM_GLOBAL_TUNING["min_body_ratio_confirmation"],
            0.0,
            0.95,
        )
        tuned["min_close_position"] = breakout._clamp(
            float(tuned["min_close_position"]) + PREMIUM_GLOBAL_TUNING["min_close_position"],
            0.0,
            0.95,
        )
        tuned["min_rr"] = float(tuned["min_rr"]) + PREMIUM_GLOBAL_TUNING["min_rr"]

    if str(direction).upper() == "LONG":
        direction_tuning = LONG_DIRECTIONAL_TUNING.get(profile_name, {})
        tuned["min_sweep_atr"] = float(tuned["min_sweep_atr"]) + float(direction_tuning.get("min_sweep_atr", 0.0))
        tuned["min_confirm_rel_volume"] = float(tuned["min_confirm_rel_volume"]) + float(direction_tuning.get("min_confirm_rel_volume", 0.0))
        tuned["min_body_ratio_confirmation"] = breakout._clamp(
            float(tuned["min_body_ratio_confirmation"]) + float(direction_tuning.get("min_body_ratio_confirmation", 0.0)),
            0.0,
            0.95,
        )
        tuned["min_close_position"] = breakout._clamp(
            float(tuned["min_close_position"]) + float(direction_tuning.get("min_close_position", 0.0)),
            0.0,
            0.97,
        )
        tuned["min_rr"] = float(tuned["min_rr"]) + float(direction_tuning.get("min_rr", 0.0))
        tuned["htf_required_score"] = max(
            int(tuned.get("htf_required_score", 1)),
            int(profile.get("htf_required_score", 1)) + int(direction_tuning.get("htf_required_score", 0)),
        )
        tuned["htf_price_tolerance"] = max(0.0, float(tuned["htf_price_tolerance"]) * float(direction_tuning.get("htf_price_tolerance_mul", 1.0)))
        tuned["htf_trend_tolerance"] = max(0.0, float(tuned["htf_trend_tolerance"]) * float(direction_tuning.get("htf_trend_tolerance_mul", 1.0)))

    return tuned


def _directional_min_raw_score(profile_name: str, direction: str) -> float:
    minimum = _min_raw_score_for_profile(profile_name)
    normalized_profile = str(profile_name or "").strip().lower()
    if normalized_profile == "premium":
        minimum += PREMIUM_SCORE_FLOOR_BONUS
    if str(direction).upper() == "LONG":
        minimum += float(LONG_SCORE_FLOOR_BONUS.get(normalized_profile, 0.0))
    return float(minimum)


def _directional_context_ok(df: pd.DataFrame, direction: str, profile: Dict) -> bool:
    if df is None or df.empty or len(df) < 2:
        return True

    profile_name = _profile_name(profile)
    last = df.iloc[-1]
    prev = df.iloc[-2]

    close = float(last["close"])
    prev_close = float(prev["close"])
    high = float(last["high"])
    prev_high = float(prev["high"])
    low = float(last["low"])
    prev_low = float(prev["low"])
    ema20 = float(last["ema20"])
    prev_ema20 = float(prev["ema20"])
    rel_volume = float(last.get("rel_volume") or 0.0)

    if str(direction).upper() == "SHORT":
        if profile_name != "premium":
            return True
        checks = [
            close <= (ema20 * 1.002),
            close <= (prev_close * 1.002) or low <= prev_low,
            rel_volume >= float(profile.get("min_confirm_rel_volume", 0.0)),
        ]
        return sum(bool(flag) for flag in checks) >= 2

    checks = [
        close >= (ema20 * (0.999 if profile_name == "free" else 1.0)),
        ema20 >= (prev_ema20 * (0.999 if profile_name == "free" else 0.9995 if profile_name == "plus" else 1.0)),
        close >= (prev_close * (0.999 if profile_name == "free" else 1.0)) or high >= prev_high,
        rel_volume >= float(profile.get("min_confirm_rel_volume", 0.0)),
    ]
    required = 2 if profile_name == "free" else 3
    return sum(bool(flag) for flag in checks) >= required

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    frame = df.copy()
    frame["ema20"] = frame["close"].astype(float).ewm(span=20, adjust=False, min_periods=20).mean()
    frame["ema50"] = frame["close"].astype(float).ewm(span=50, adjust=False, min_periods=30).mean()
    high = frame["high"].astype(float)
    low = frame["low"].astype(float)
    close = frame["close"].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low).abs(),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    frame["atr"] = tr.rolling(window=14, min_periods=14).mean()
    frame["atr_pct"] = frame["atr"] / close.replace(0, pd.NA)
    frame["body"] = (close - frame["open"].astype(float)).abs()
    frame["range"] = (high - low).replace(0, 1e-9)
    frame["body_ratio"] = frame["body"] / frame["range"]
    frame["rel_volume"] = frame["volume"].astype(float) / frame["volume"].astype(float).rolling(20, min_periods=5).mean().replace(0, pd.NA)
    frame["upper_wick"] = (high - frame[["open", "close"]].max(axis=1)).clip(lower=0.0) / frame["range"]
    frame["lower_wick"] = (frame[["open", "close"]].min(axis=1) - low).clip(lower=0.0) / frame["range"]
    frame["close_position"] = (close - low) / frame["range"]
    return frame



def _indicators_ready(last: pd.Series) -> bool:
    try:
        for key in ["atr", "atr_pct", "ema20", "ema50", "body_ratio", "rel_volume", "close_position"]:
            value = float(last.get(key))
            if not pd.notna(value):
                return False
        return True
    except Exception:
        return False



def _closed_15m_frame(df_15m: pd.DataFrame) -> pd.DataFrame:
    if df_15m is None:
        return pd.DataFrame()
    if df_15m.empty or "close_time" not in df_15m.columns:
        return df_15m.copy()

    frame = df_15m.copy()
    close_time = frame["close_time"]
    if pd.api.types.is_numeric_dtype(close_time):
        close_time = pd.to_datetime(close_time, unit="ms", utc=True, errors="coerce")
    else:
        close_time = pd.to_datetime(close_time, utc=True, errors="coerce")

    valid_times = close_time.notna()
    if not valid_times.any():
        # Si la fuente no aporta timestamps utilizables, conservamos el
        # comportamiento anterior en vez de descartar datos silenciosamente.
        return frame

    now_utc = pd.Timestamp.now(tz="UTC")
    closed = frame.loc[valid_times & (close_time <= now_utc)].copy()
    if not closed.empty:
        return closed
    if len(frame) > 1:
        return frame.iloc[:-1].copy()
    return frame.copy()



def _min_raw_score_for_profile(profile_name: str) -> float:
    if profile_name == _profile_name(PREMIUM_PROFILE):
        return PREMIUM_RAW_SCORE_MIN
    if profile_name == PLUS_PROFILE["name"]:
        return PLUS_RAW_SCORE_MIN
    return FREE_RAW_SCORE_MIN



def _higher_timeframe_context_ok(df_1h: pd.DataFrame, direction: str, profile: Dict) -> bool:
    if df_1h is None or df_1h.empty or len(df_1h) < 40:
        return True
    enriched = add_indicators(_closed_15m_frame(df_1h))
    if enriched is None or enriched.empty:
        return True
    last = enriched.iloc[-1]
    if not _indicators_ready(last):
        return True
    prev = enriched.iloc[-2] if len(enriched) >= 2 else last
    recent = enriched.tail(3)

    close = float(last["close"])
    ema20 = float(last["ema20"])
    ema50 = float(last["ema50"])
    prev_close = float(prev["close"])
    prev_ema20 = float(prev["ema20"])
    tolerance_price = max(0.0, float(profile.get("htf_price_tolerance", 0.0)))
    tolerance_trend = max(0.0, float(profile.get("htf_trend_tolerance", 0.0)))
    required_score = max(1, int(profile.get("htf_required_score", 1)))

    if direction == "LONG":
        price_support = close >= (ema20 * (1.0 - tolerance_price))
        trend_support = ema20 >= (ema50 * (1.0 - tolerance_trend))
        momentum_support = close >= prev_close or ema20 >= (prev_ema20 * 0.999)
        reclaim_support = bool((recent["close"].astype(float) >= (recent["ema20"].astype(float) * (1.0 - tolerance_price))).any())
        score = sum(bool(flag) for flag in (price_support, trend_support, momentum_support, reclaim_support))
        if _profile_name(profile) == "premium" and not price_support and score < max(1, required_score):
            return False
        return score >= required_score

    price_support = close <= (ema20 * (1.0 + tolerance_price))
    trend_support = ema20 <= (ema50 * (1.0 + tolerance_trend))
    momentum_support = close <= prev_close or ema20 <= (prev_ema20 * 1.001)
    reclaim_support = bool((recent["close"].astype(float) <= (recent["ema20"].astype(float) * (1.0 + tolerance_price))).any())
    score = sum(bool(flag) for flag in (price_support, trend_support, momentum_support, reclaim_support))
    if _profile_name(profile) == "premium" and not price_support and score < max(1, required_score):
        return False
    return score >= required_score



def _is_pivot_low(df: pd.DataFrame, idx: int, left: int, right: int) -> bool:
    low = float(df.iloc[idx]["low"])
    window = df.iloc[idx - left: idx + right + 1]
    return low <= float(window["low"].min())



def _is_pivot_high(df: pd.DataFrame, idx: int, left: int, right: int) -> bool:
    high = float(df.iloc[idx]["high"])
    window = df.iloc[idx - left: idx + right + 1]
    return high >= float(window["high"].max())



def _select_liquidity_zone(df: pd.DataFrame, direction: str, profile: Dict) -> Optional[Dict]:
    if len(df) < max(LIQUIDITY_LOOKBACK, LIQUIDITY_PIVOT_LEFT + LIQUIDITY_PIVOT_RIGHT + 5):
        return None
    frame = df.iloc[-(LIQUIDITY_LOOKBACK + LIQUIDITY_PIVOT_RIGHT + 3):-2].copy()
    if frame.empty:
        return None
    atr_now = float(df.iloc[-1]["atr"])
    tolerance = max(1e-9, atr_now * float(profile["liquidity_tolerance_atr"]))
    pivot_rows: List[Dict] = []
    for idx in range(LIQUIDITY_PIVOT_LEFT, len(frame) - LIQUIDITY_PIVOT_RIGHT):
        absolute_idx = int(frame.index[idx])
        if direction == "LONG" and _is_pivot_low(frame.reset_index(drop=True), idx, LIQUIDITY_PIVOT_LEFT, LIQUIDITY_PIVOT_RIGHT):
            pivot_rows.append({"price": float(frame.iloc[idx]["low"]), "index": absolute_idx})
        elif direction == "SHORT" and _is_pivot_high(frame.reset_index(drop=True), idx, LIQUIDITY_PIVOT_LEFT, LIQUIDITY_PIVOT_RIGHT):
            pivot_rows.append({"price": float(frame.iloc[idx]["high"]), "index": absolute_idx})
    if not pivot_rows:
        if direction == "LONG":
            fallback_price = float(frame["low"].tail(12).min())
        else:
            fallback_price = float(frame["high"].tail(12).max())
        return {"price": fallback_price, "count": 1, "latest_index": int(frame.index[-1]), "tolerance": tolerance}

    pivot_rows.sort(key=lambda item: item["price"])
    clusters: List[Dict] = []
    for pivot in pivot_rows:
        matched = None
        for cluster in clusters:
            if abs(float(cluster["price"]) - float(pivot["price"])) <= tolerance:
                matched = cluster
                break
        if matched is None:
            clusters.append({
                "price": float(pivot["price"]),
                "count": 1,
                "latest_index": int(pivot["index"]),
                "members": [pivot],
            })
        else:
            matched["members"].append(pivot)
            matched["count"] += 1
            matched["latest_index"] = max(int(matched["latest_index"]), int(pivot["index"]))
            matched["price"] = sum(float(member["price"]) for member in matched["members"]) / len(matched["members"])

    clusters.sort(key=lambda c: (int(c["count"]), int(c["latest_index"])), reverse=True)
    best = clusters[0]
    return {
        "price": breakout._round_price_dynamic(float(best["price"])),
        "count": int(best["count"]),
        "latest_index": int(best["latest_index"]),
        "tolerance": tolerance,
    }



def _recovery_candle_ok(last: pd.Series, direction: str, level: float, profile: Dict) -> bool:
    body_ratio = float(last["body_ratio"])
    rel_volume = float(last.get("rel_volume") or 0.0)
    close = float(last["close"])
    close_position = float(last["close_position"])
    threshold = float(profile["min_close_position"])
    if direction == "LONG":
        reclaim_ok = close > level
        score = sum([
            body_ratio >= float(profile["min_body_ratio_recovery"]),
            rel_volume >= float(profile["min_rel_volume"]),
            close_position >= threshold,
        ])
    else:
        reclaim_ok = close < level
        score = sum([
            body_ratio >= float(profile["min_body_ratio_recovery"]),
            rel_volume >= float(profile["min_rel_volume"]),
            (1.0 - close_position) >= threshold,
        ])
    return reclaim_ok and score >= 2



def _confirmation_candle_ok(last: pd.Series, direction: str, level: float, profile: Dict) -> bool:
    body_ratio = float(last["body_ratio"])
    rel_volume = float(last.get("rel_volume") or 0.0)
    close = float(last["close"])
    close_position = float(last["close_position"])
    threshold = max(0.46, float(profile["min_close_position"]) - 0.07)
    if direction == "LONG":
        reclaim_ok = close >= level
        score = sum([
            body_ratio >= float(profile["min_body_ratio_confirmation"]),
            rel_volume >= float(profile["min_confirm_rel_volume"]),
            close_position >= threshold,
        ])
    else:
        reclaim_ok = close <= level
        score = sum([
            body_ratio >= float(profile["min_body_ratio_confirmation"]),
            rel_volume >= float(profile["min_confirm_rel_volume"]),
            (1.0 - close_position) >= threshold,
        ])
    return reclaim_ok and score >= 2



def _ema_reclaim_ok(last: pd.Series, direction: str) -> bool:
    close = float(last["close"])
    ema20 = float(last["ema20"])
    ema50 = float(last["ema50"])
    if direction == "LONG":
        return close >= ema20 and close >= ema50 * 0.998
    return close <= ema20 and close <= ema50 * 1.002



def _find_recent_sweep(df: pd.DataFrame, direction: str, level: float, profile: Dict) -> Optional[Dict]:
    tail = df.tail(SWEEP_SEARCH_BARS).copy()
    if tail.empty:
        return None
    atr_now = float(df.iloc[-1]["atr"])
    min_sweep_distance = atr_now * float(profile["min_sweep_atr"])
    if direction == "LONG":
        low_price = float(tail["low"].min())
        idx = int(tail["low"].idxmin())
        candle = df.loc[idx]
        wick_ratio = float(candle.get("lower_wick") or 0.0)
        swept_hard = low_price < (level - min_sweep_distance)
        swept_soft = low_price <= (level + (atr_now * 0.025)) and wick_ratio >= 0.22
        if not (swept_hard or swept_soft):
            return None
        if float(candle["close"]) <= (level - atr_now * 0.035):
            return None
        return {
            "index": idx,
            "extreme_price": breakout._round_price_dynamic(low_price),
            "sweep_distance_atr": round((level - low_price) / max(atr_now, 1e-9), 4),
            "wick_ratio": round(wick_ratio, 4),
            "sweep_mode": "hard" if swept_hard else "soft",
        }
    high_price = float(tail["high"].max())
    idx = int(tail["high"].idxmax())
    candle = df.loc[idx]
    wick_ratio = float(candle.get("upper_wick") or 0.0)
    swept_hard = high_price > (level + min_sweep_distance)
    swept_soft = high_price >= (level - (atr_now * 0.025)) and wick_ratio >= 0.22
    if not (swept_hard or swept_soft):
        return None
    if float(candle["close"]) >= (level + atr_now * 0.035):
        return None
    return {
        "index": idx,
        "extreme_price": breakout._round_price_dynamic(high_price),
        "sweep_distance_atr": round((high_price - level) / max(atr_now, 1e-9), 4),
        "wick_ratio": round(wick_ratio, 4),
        "sweep_mode": "hard" if swept_hard else "soft",
    }



def _safe_rr(entry_price: float, stop_loss: float, target_price: float) -> float:
    risk = abs(stop_loss - entry_price)
    if risk <= 1e-9:
        return 0.0
    return abs(target_price - entry_price) / risk


def _build_pullback_entry(
    *,
    direction: str,
    level: float,
    confirmation_close: float,
    market_price: float,
    atr_now: float,
) -> Optional[float]:
    reference_price = float(market_price or confirmation_close)
    confirmation_close = float(confirmation_close)
    level = float(level)
    atr_now = max(float(atr_now or 0.0), max(abs(reference_price) * 0.0003, 1e-9))

    reclaim_distance = abs(confirmation_close - level)
    desired_pullback = breakout._clamp(
        max(reclaim_distance * PULLBACK_RETRACE_FRACTION, atr_now * PULLBACK_ATR_MIN),
        atr_now * PULLBACK_ATR_MIN,
        atr_now * PULLBACK_ATR_MAX,
    )
    level_buffer = atr_now * PULLBACK_LEVEL_BUFFER_ATR
    market_gap = atr_now * PULLBACK_MARKET_GAP_ATR

    if str(direction).upper() == "LONG":
        floor = level + level_buffer
        upper = reference_price - market_gap
        if upper <= floor:
            return None
        candidate = max(floor, reference_price - desired_pullback)
        return breakout._round_price_dynamic(min(candidate, upper))

    ceiling = level - level_buffer
    lower = reference_price + market_gap
    if lower >= ceiling:
        return None
    candidate = min(ceiling, reference_price + desired_pullback)
    return breakout._round_price_dynamic(max(candidate, lower))


def _swing_levels(df: Optional[pd.DataFrame], direction: str, lookback: int, source: str) -> List[Dict]:
    """Niveles de liquidez donde es lógico que el precio llegue.

    LONG  -> máximos de swing (liquidez compradora/stops de shorts) por encima.
    SHORT -> mínimos de swing por debajo.
    Incluye el extremo del rango (puede ser la última vela, aún sin pivote confirmado).
    """
    if df is None or len(df) < (TARGET_PIVOT_BARS * 2 + 3):
        return []
    is_long = str(direction).upper() == "LONG"
    col = "high" if is_long else "low"
    values = df[col].astype(float).to_numpy()[-lookback:]
    n = len(values)
    k = TARGET_PIVOT_BARS
    levels: List[Dict] = []
    for i in range(k, n - k):
        window = values[i - k: i + k + 1]
        v = float(values[i])
        if (is_long and v >= float(window.max())) or ((not is_long) and v <= float(window.min())):
            levels.append({"price": v, "touches": 1, "source": source})
    extreme = float(values.max() if is_long else values.min())
    levels.append({"price": extreme, "touches": 1, "source": f"{source}-range"})
    return levels


def _cluster_levels(levels: List[Dict], direction: str, tolerance: float) -> List[Dict]:
    """Agrupa niveles casi iguales (equal highs/lows). Más toques = liquidez más fuerte."""
    if not levels:
        return []
    is_long = str(direction).upper() == "LONG"
    ordered = sorted(levels, key=lambda item: item["price"])
    clusters: List[List[Dict]] = [[ordered[0]]]
    for item in ordered[1:]:
        if item["price"] - clusters[-1][-1]["price"] <= tolerance:
            clusters[-1].append(item)
        else:
            clusters.append([item])
    merged: List[Dict] = []
    for members in clusters:
        prices = [float(m["price"]) for m in members]
        # El primer nivel que el precio alcanza: el más bajo (LONG) o el más alto (SHORT).
        price = min(prices) if is_long else max(prices)
        sources = sorted({str(m["source"]) for m in members})
        merged.append({
            "price": price,
            "touches": int(sum(int(m["touches"]) for m in members)),
            "source": "+".join(sources),
        })
    return merged


def _build_structural_trade(
    *,
    direction: str,
    entry_price: float,
    atr_now: float,
    sweep_extreme_price: float,
    df_15m: pd.DataFrame,
    df_1h: Optional[pd.DataFrame] = None,
) -> Tuple[Optional[Dict], Optional[str]]:
    """SL y TP calculados por la estrategia a partir de la estructura del mercado.

    Devuelve (trade, None) o (None, motivo_de_rechazo).
    """
    is_long = str(direction).upper() == "LONG"
    entry_price = float(entry_price)
    atr_now = max(float(atr_now or 0.0), max(abs(entry_price) * 0.0003, 1e-9))
    sweep_extreme_price = float(sweep_extreme_price)

    # ---- Stop Loss: detrás del extremo del barrido + colchón de volatilidad ----
    buffer_distance = atr_now * max(SL_BUFFER_ATR, 0.0)
    stop_loss = (sweep_extreme_price - buffer_distance) if is_long else (sweep_extreme_price + buffer_distance)
    risk_distance = (entry_price - stop_loss) if is_long else (stop_loss - entry_price)
    if risk_distance <= 0:
        return None, "liquidity_stop_invalid"
    # El stop nunca puede quedar dentro de la zona de entrada: usamos el mismo ancho
    # de zona que se publicará para garantizar SL fuera de ella (y TP1 más allá).
    zone_half = breakout._clamp(
        atr_now * ENTRY_ZONE_HALF_ATR,
        abs(entry_price) * ENTRY_ZONE_MIN_PCT,
        abs(entry_price) * ENTRY_ZONE_MAX_PCT,
    )
    min_risk = max(atr_now * max(SL_MIN_RISK_ATR, 0.0), zone_half * 1.5)
    if risk_distance < min_risk:
        # El extremo está pegado a la entrada: el stop quedaría dentro del ruido normal.
        stop_loss = (entry_price - min_risk) if is_long else (entry_price + min_risk)
        risk_distance = min_risk
    if risk_distance > max(atr_now * SL_MAX_RISK_ATR, min_risk):
        return None, "liquidity_stop_too_wide"

    # ---- Take Profits: siguientes pools de liquidez en la dirección del trade ----
    # TP1 mantiene exactamente la ventana estructural anterior para no mover
    # innecesariamente el objetivo cercano de las señales existentes.
    primary_levels = _swing_levels(df_15m, direction, TARGET_LOOKBACK_15M, "15M")
    primary_levels += _swing_levels(df_1h, direction, TARGET_LOOKBACK_1H, "1H")
    primary_levels = _cluster_levels(primary_levels, direction, atr_now * max(TARGET_CLUSTER_ATR, 0.0))

    frontrun = atr_now * max(TARGET_FRONTRUN_ATR, 0.0)
    max_reach = atr_now * max(TARGET_MAX_ATR, 0.5)

    def target_candidates(levels: List[Dict]) -> List[Dict]:
        candidates: List[Dict] = []
        for level in levels:
            price = float(level["price"]) - frontrun if is_long else float(level["price"]) + frontrun
            reach = (price - entry_price) if is_long else (entry_price - price)
            if reach <= 0 or reach > max_reach:
                continue
            candidates.append({**level, "target": price, "reach": reach, "rr": reach / risk_distance})
        return sorted(candidates, key=lambda item: item["reach"])

    primary_candidates = target_candidates(primary_levels)

    tp1 = next((c for c in primary_candidates if c["rr"] >= MIN_PUBLISH_TP1_RR), None)
    if tp1 is None:
        return None, "liquidity_no_structural_target"

    # TP2 is selected from a wider historical window of real 15m/1h swing
    # levels. The already-selected TP1 is not changed. This improves the chance
    # of finding a second structural objective without inventing a fixed 1.5R/2R.
    extended_15m = max(TARGET_LOOKBACK_15M, int(round(TARGET_LOOKBACK_15M * TARGET_TP2_LOOKBACK_MULTIPLIER)))
    extended_1h = max(TARGET_LOOKBACK_1H, int(round(TARGET_LOOKBACK_1H * TARGET_TP2_LOOKBACK_MULTIPLIER)))
    tp2_levels = _swing_levels(df_15m, direction, extended_15m, "15M-EXT")
    tp2_levels += _swing_levels(df_1h, direction, extended_1h, "1H-EXT")
    # Combine current and extended levels so equal/nearby highs/lows still form
    # one liquidity pool and cannot be advertised as two separate objectives.
    tp2_levels = _cluster_levels(
        primary_levels + tp2_levels,
        direction,
        atr_now * max(TARGET_CLUSTER_ATR, 0.0),
    )
    secondary_candidates = target_candidates(tp2_levels)
    min_gap = atr_now * max(TARGET_MIN_GAP_ATR, 0.0)
    tp2 = next((c for c in secondary_candidates if c["reach"] - tp1["reach"] >= min_gap), None)

    chosen = [tp1] + ([tp2] if tp2 is not None else [])
    take_profits = [breakout._round_price_dynamic(c["target"]) for c in chosen]
    return {
        "stop_loss": breakout._round_price_dynamic(stop_loss),
        "take_profits": take_profits,
        "risk_atr": round(risk_distance / atr_now, 4),
        "targets": [
            {"price": take_profits[i], "rr": round(c["rr"], 2), "touches": c["touches"], "source": c["source"]}
            for i, c in enumerate(chosen)
        ],
    }, None


def _component(name: str, points: float, value: float, passed: bool) -> Dict:
    return {
        "name": name,
        "points": round(points if passed else 0.0, 2),
        "value": round(float(value), 4),
        "passed": bool(passed),
    }



def _score_components(last: pd.Series, direction: str, zone: Dict, sweep: Dict, barrier_rr: float, profile: Dict) -> List[Dict]:
    rel_volume = float(last.get("rel_volume") or 0.0)
    body_ratio = float(last.get("body_ratio") or 0.0)
    close_position = float(last.get("close_position") or 0.0)
    confirmation_volume_ok = rel_volume >= float(profile["min_confirm_rel_volume"])
    close_pos_value = close_position if direction == "LONG" else (1.0 - close_position)

    return [
        _component("liquidity_zone", 18.0, float(zone.get("count") or 0.0), float(zone.get("count") or 0.0) >= 1.0),
        _component("minimum_sweep", 18.0, float(sweep.get("sweep_distance_atr") or 0.0), float(sweep.get("sweep_distance_atr") or 0.0) >= float(profile["min_sweep_atr"])),
        _component("recovery_close", 16.0, body_ratio, body_ratio >= float(profile["min_body_ratio_recovery"])),
        _component("confirmation_volume", 14.0, rel_volume, confirmation_volume_ok),
        _component("close_position", 12.0, close_pos_value, close_pos_value >= float(profile["min_close_position"])),
        _component("ema_reclaim", 10.0, float(last["close"]), True),
        _component("barrier_room_rr", 12.0, barrier_rr, barrier_rr >= float(profile["min_rr"])),
    ]



def _sum_components(components: List[Dict]) -> float:
    total = sum(float(item.get("points") or 0.0) for item in components)
    return round(breakout._clamp(total, 0.0, 100.0), 2)



def _evaluate_direction(
    df: pd.DataFrame,
    df_1h: pd.DataFrame,
    df_5m: Optional[pd.DataFrame],
    direction: str,
    profile: Dict,
    *,
    current_market_price: Optional[float],
    debug_counts: Optional[Dict[str, int]] = None,
) -> Optional[Tuple[Dict, Tuple[float, float, float]]]:
    tuned_profile = _directional_profile(profile, direction)
    diagnostic_prefix = f"{str(tuned_profile.get('name') or 'unknown').lower()}[{_profile_name(tuned_profile)}]_{str(direction).lower()}"

    def reject(reason: str) -> None:
        _record_reject(debug_counts, f"{diagnostic_prefix}_{reason}")

    if len(df) < MIN_HISTORY_BARS:
        reject("liquidity_insufficient_history")
        return None

    last = df.iloc[-1]
    if not _indicators_ready(last):
        reject("liquidity_indicator_warmup")
        return None

    atr_pct = float(last["atr_pct"])
    if not (float(tuned_profile["atr_pct_min"]) <= atr_pct <= float(tuned_profile["atr_pct_max"])):
        reject("liquidity_atr_pct")
        return None

    if not _higher_timeframe_context_ok(df_1h, direction, tuned_profile):
        reject("liquidity_htf_context")
        return None

    zone = _select_liquidity_zone(df, direction, tuned_profile)
    if not zone:
        reject("liquidity_zone_missing")
        return None

    level = float(zone["price"])
    sweep = _find_recent_sweep(df, direction, level, tuned_profile)
    if not sweep:
        reject("liquidity_minimum_sweep")
        return None

    if not _recovery_candle_ok(last, direction, level, tuned_profile):
        reject("liquidity_recovery_close")
        return None

    if not _confirmation_candle_ok(last, direction, level, tuned_profile):
        reject("liquidity_confirmation")
        return None

    if not _directional_context_ok(df, direction, tuned_profile):
        reject("liquidity_directional_context")
        return None

    if not _ema_reclaim_ok(last, direction):
        reject("liquidity_ema_reclaim")
        return None

    current_reference_price = float(current_market_price or last["close"])
    if current_reference_price <= 0:
        current_reference_price = float(last["close"])

    atr_now = float(last["atr"])
    entry_price = _build_pullback_entry(
        direction=direction,
        level=level,
        confirmation_close=float(last["close"]),
        market_price=current_reference_price,
        atr_now=atr_now,
    )
    if entry_price is None:
        reject("liquidity_pullback_geometry")
        return None

    trade, trade_reject = _build_structural_trade(
        direction=direction,
        entry_price=entry_price,
        atr_now=atr_now,
        sweep_extreme_price=float(sweep.get("extreme_price") or level),
        df_15m=df,
        df_1h=df_1h,
    )
    if trade is None:
        reject(trade_reject or "liquidity_trade_structure")
        return None
    stop_loss = float(trade["stop_loss"])
    take_profits = list(trade["take_profits"])

    # "Espacio" disponible = R:R hasta el último objetivo estructural.
    barrier_rr = _safe_rr(entry_price, stop_loss, take_profits[-1])
    if barrier_rr < float(tuned_profile["min_rr"]):
        reject("liquidity_barrier_room")
        return None

    components = _score_components(last, direction, zone, sweep, barrier_rr, tuned_profile)
    raw_score = _sum_components(components)
    if raw_score < _directional_min_raw_score(_profile_name(tuned_profile), direction):
        reject("liquidity_score_floor")
        return None

    entry_model_price = float(last["close"])
    payload = {
        "direction": direction,
        "entry_price": breakout._round_price_dynamic(entry_price),
        "stop_loss": stop_loss,
        "take_profits": take_profits,
        "risk_atr": trade["risk_atr"],
        "target_model": TARGET_MODEL_NAME,
        "targets": trade["targets"],
        "score": raw_score,
        "raw_score": raw_score,
        "normalized_score": raw_score,
        "components": components,
        "raw_components": components,
        "normalized_components": components,
        "timeframes": ["15M", "5M"],
        "setup_group": str(tuned_profile["name"]),
        "delivery_plan": str(tuned_profile["name"]),
        "filter_profile": _profile_name(tuned_profile),
        "atr_pct": round(atr_pct, 6),
        "atr_value": breakout._round_price_dynamic(atr_now),
        "score_profile": str(tuned_profile["name"]),
        "score_calibration": SCORE_CALIBRATION_VERSION,
        "send_mode": SEND_MODE_PENDING_ENTRY,
        "entry_model_price": breakout._round_price_dynamic(entry_model_price),
        "entry_sent_price": breakout._round_price_dynamic(current_reference_price),
        "setup_stage": SETUP_STAGE_WAITING_PULLBACK,
        "entry_model": ENTRY_MODEL_NAME,
        "higher_tf_context": {"liquidity_zone": level, "sweep_distance_atr": sweep.get("sweep_distance_atr")},
        "liquidity_zone": breakout._round_price_dynamic(level),
        "liquidity_sweep_distance_atr": sweep.get("sweep_distance_atr"),
        "liquidity_sweep_extreme_price": sweep.get("extreme_price"),
        "liquidity_sweep_at": df.loc[sweep["index"]].get("close_time"),
        "strategy_runtime": {
            "post_fill_invalidation": {
                "minutes": POST_FILL_INVALIDATION_MINUTES,
                "min_tp1_progress_pct": POST_FILL_MIN_TP1_PROGRESS_PCT,
                "reason": "liquidity_no_followthrough",
            }
        },
    }
    ranking = (raw_score, barrier_rr, float(zone.get("count") or 0.0))
    return payload, ranking




def _bars_since_index(df: pd.DataFrame, index_label) -> Optional[int]:
    """Return closed bars elapsed since an indexed event, robust to non-integer indexes."""
    try:
        positions = [i for i, label in enumerate(df.index) if label == index_label]
        if not positions:
            return None
        return max(0, (len(df) - 1) - positions[-1])
    except Exception:
        return None


def _early_structure_shift_ok(df_5m: pd.DataFrame, direction: str, sweep_index) -> bool:
    """Detect the first simple 5m market-structure shift after a liquidity sweep.

    LONG: confirmation closes above the previous closed candle high.
    SHORT: confirmation closes below the previous closed candle low.
    The sweep candle itself may be the previous candle.
    """
    if df_5m is None or len(df_5m) < 2:
        return False
    trigger = df_5m.iloc[-1]
    prev = df_5m.iloc[-2]
    direction = str(direction).upper()
    if direction == "LONG":
        return float(trigger["close"]) > float(prev["high"])
    return float(trigger["close"]) < float(prev["low"])


def _build_dynamic_entry_zone(entry_price: float, atr_now: float) -> Dict[str, float]:
    """Build a volatility-aware Telegram entry range around the model entry."""
    entry_price = float(entry_price)
    atr_now = max(float(atr_now or 0.0), abs(entry_price) * 0.0003, 1e-9)
    half_width = breakout._clamp(
        atr_now * ENTRY_ZONE_HALF_ATR,
        abs(entry_price) * ENTRY_ZONE_MIN_PCT,
        abs(entry_price) * ENTRY_ZONE_MAX_PCT,
    )
    low = breakout._round_price_dynamic(entry_price - half_width)
    high = breakout._round_price_dynamic(entry_price + half_width)
    return {"low": min(low, high), "high": max(low, high)}


def _tp1_rr(entry_price: float, stop_loss: float, take_profits: List[float]) -> float:
    if not take_profits:
        return 0.0
    return _safe_rr(float(entry_price), float(stop_loss), float(take_profits[0]))


def _restore_observed_sweep(df_5m: pd.DataFrame, direction: str, level: float,
                            observation: Mapping, profile: Optional[Mapping] = None) -> Optional[Dict]:
    """Revalidate stored 5m sweep on the original closed candle and level.

    Never trusts stored state alone: changed 15m levels, vanished/edited OHLC,
    or late confirmations are rejected. Historic events cannot reset their age.
    """
    event_at = observation.get("event_at")
    if event_at is None or df_5m is None or df_5m.empty:
        return None
    try:
        event_ts = pd.Timestamp(event_at)
        event_ts = event_ts.tz_localize("UTC") if event_ts.tzinfo is None else event_ts.tz_convert("UTC")
        times = pd.to_datetime(df_5m["close_time"], utc=True, errors="coerce")
        positions = [i for i, ts in enumerate(times) if pd.notna(ts) and abs((ts - event_ts).total_seconds()) <= 1]
        if not positions:
            return None
        pos = positions[-1]
        stored_level = float(observation.get("level") or 0)
        extreme = float(observation.get("extreme_price") or 0)
        atr = float(df_5m.iloc[-1]["atr"])
        candle = df_5m.iloc[pos]
        actual_extreme = float(candle["low" if direction == "LONG" else "high"])
        if abs(level - stored_level) > atr * 0.25:
            return None
        if abs(extreme - actual_extreme) > max(abs(extreme) * 0.000001, 1e-8):
            return None
        if profile is not None:
            # The selected 15m liquidity zone can change between cycles.
            # Reapply the ORIGINAL wick/reclaim criteria against the new zone;
            # proximity of the levels alone cannot justify a trade.
            distance = atr * float(profile["min_sweep_atr"])
            if direction == "LONG":
                hard = extreme < level - distance
                soft = extreme <= level + atr * 0.025 and float(candle.get("lower_wick") or 0) >= 0.22
                recovered = float(candle["close"]) > level - atr * 0.035
            else:
                hard = extreme > level + distance
                soft = extreme >= level - atr * 0.025 and float(candle.get("upper_wick") or 0) >= 0.22
                recovered = float(candle["close"]) < level + atr * 0.035
            if not ((hard or soft) and recovered):
                return None
        if len(df_5m) - 1 - pos > MAX_BARS_AFTER_SWEEP:
            return None
        return {
            "index": df_5m.index[pos],
            "extreme_price": extreme,
            "sweep_distance_atr": float(observation.get("sweep_distance_atr") or 0),
            "wick_ratio": float(observation.get("wick_ratio") or 0),
            "sweep_mode": observation.get("sweep_mode", "cached"),
        }
    except (ValueError, TypeError, KeyError, OverflowError):
        return None


def _evaluate_direction_5m_execution(
    df_15m: pd.DataFrame,
    df_1h: pd.DataFrame,
    df_5m: pd.DataFrame,
    direction: str,
    profile: Dict,
    *,
    current_market_price: Optional[float],
    debug_counts: Optional[Dict[str, int]] = None,
    observed_sweep: Optional[Mapping] = None,
    on_sweep: Optional[Callable] = None,
) -> Optional[Tuple[Dict, Tuple[float, float, float]]]:
    """
    Detect structure/liquidity on 15m, but execute from a CLOSED 5m candle.

    This removes the old latency where a valid sweep had to wait for a complete
    15m confirmation candle. The open 5m candle is used only as a live price
    reference, never as confirmation, so the trigger does not repaint.
    """
    tuned_profile = _directional_profile(profile, direction)
    diagnostic_prefix = f"{str(tuned_profile.get('name') or 'unknown').lower()}[{_profile_name(tuned_profile)}]_{str(direction).lower()}"

    def reject(reason: str) -> None:
        _record_reject(debug_counts, f"{diagnostic_prefix}_{reason}")

    if len(df_15m) < MIN_HISTORY_BARS:
        reject("liquidity_insufficient_history")
        return None
    if df_5m is None or len(df_5m) < max(30, SWEEP_SEARCH_BARS + 5):
        reject("execution_5m_insufficient_history")
        return None

    structure_last = df_15m.iloc[-1]
    trigger_last = df_5m.iloc[-1]
    if not _indicators_ready(structure_last) or not _indicators_ready(trigger_last):
        reject("liquidity_indicator_warmup")
        return None

    atr_pct = float(structure_last["atr_pct"])
    if not (float(tuned_profile["atr_pct_min"]) <= atr_pct <= float(tuned_profile["atr_pct_max"])):
        reject("liquidity_atr_pct")
        return None
    if not _higher_timeframe_context_ok(df_1h, direction, tuned_profile):
        reject("liquidity_htf_context")
        return None

    zone = _select_liquidity_zone(df_15m, direction, tuned_profile)
    if not zone:
        reject("liquidity_zone_missing")
        return None
    level = float(zone["price"])

    # Sweep + recovery + confirmation happen on 5m for earlier delivery.
    sweep = _find_recent_sweep(df_5m, direction, level, tuned_profile)
    if sweep is not None and on_sweep is not None:
        candle = df_5m.loc[sweep["index"]]
        on_sweep(_profile_name(tuned_profile), direction, sweep, level, candle.get("close_time"))
    if sweep is None and observed_sweep is not None:
        sweep = _restore_observed_sweep(df_5m, direction, level, observed_sweep, tuned_profile)
        if sweep:
            _record_reject(debug_counts, f"{diagnostic_prefix}_observed_sweep_reused")
    if not sweep:
        reject("execution_5m_sweep")
        return None

    bars_after_sweep = _bars_since_index(df_5m, sweep.get("index"))
    if bars_after_sweep is None or bars_after_sweep > MAX_BARS_AFTER_SWEEP:
        reject("execution_confirmation_too_late")
        return None
    if REQUIRE_EARLY_STRUCTURE_SHIFT and not _early_structure_shift_ok(df_5m, direction, sweep.get("index")):
        reject("execution_structure_shift_missing")
        return None

    if not _recovery_candle_ok(trigger_last, direction, level, tuned_profile):
        reject("execution_5m_recovery")
        return None
    if not _confirmation_candle_ok(trigger_last, direction, level, tuned_profile):
        reject("execution_5m_confirmation")
        return None
    if not _directional_context_ok(df_15m, direction, tuned_profile):
        reject("liquidity_directional_context")
        return None
    if not _ema_reclaim_ok(trigger_last, direction):
        reject("execution_5m_ema_reclaim")
        return None

    confirmation_close = float(trigger_last["close"])
    current_reference_price = float(current_market_price or confirmation_close)
    if current_reference_price <= 0:
        current_reference_price = confirmation_close

    # Use 15m ATR for stable distance/risk sizing, but refuse chased entries.
    atr_now = float(structure_last["atr"])
    sweep_extreme = float(sweep.get("extreme_price") or level)
    sweep_to_market_atr = abs(current_reference_price - sweep_extreme) / max(atr_now, 1e-9)
    if sweep_to_market_atr > MAX_SWEEP_TO_MARKET_ATR:
        reject("execution_sweep_move_consumed")
        return None

    max_extension = max(atr_now * MAX_SIGNAL_EXTENSION_ATR, abs(current_reference_price) * 0.0003)
    if direction == "LONG" and current_reference_price > confirmation_close + max_extension:
        reject("execution_price_extended")
        return None
    if direction == "SHORT" and current_reference_price < confirmation_close - max_extension:
        reject("execution_price_extended")
        return None

    entry_price = _build_pullback_entry(
        direction=direction,
        level=level,
        confirmation_close=confirmation_close,
        market_price=current_reference_price,
        atr_now=atr_now,
    )
    if entry_price is None:
        reject("liquidity_pullback_geometry")
        return None

    trade, trade_reject = _build_structural_trade(
        direction=direction,
        entry_price=entry_price,
        atr_now=atr_now,
        sweep_extreme_price=float(sweep.get("extreme_price") or level),
        df_15m=df_15m,
        df_1h=df_1h,
    )
    if trade is None:
        reject(trade_reject or "liquidity_trade_structure")
        return None
    stop_loss = float(trade["stop_loss"])
    take_profits = list(trade["take_profits"])

    publish_tp1_rr = _tp1_rr(entry_price, stop_loss, take_profits)
    if publish_tp1_rr < MIN_PUBLISH_TP1_RR:
        reject("execution_tp1_rr_too_low")
        return None

    entry_zone = _build_dynamic_entry_zone(entry_price, atr_now)

    # La señal debe nacer ANTES del primer toque de la zona proyectada.
    # Además dejamos una distancia mínima para absorber latencia de scanner/Telegram
    # y una máxima para no publicar retrocesos demasiado especulativos.
    if direction == "LONG":
        preentry_gap = current_reference_price - float(entry_zone["high"])
    else:
        preentry_gap = float(entry_zone["low"]) - current_reference_price
    preentry_distance_atr = preentry_gap / max(atr_now, 1e-9)
    if preentry_gap <= 0:
        reject("execution_entry_zone_already_touched")
        return None
    if preentry_distance_atr < MIN_PREENTRY_DISTANCE_ATR:
        reject("execution_entry_zone_too_close")
        return None
    if preentry_distance_atr > MAX_PREENTRY_DISTANCE_ATR:
        reject("execution_entry_zone_too_far")
        return None

    barrier_rr = _safe_rr(entry_price, stop_loss, take_profits[-1])
    if barrier_rr < float(tuned_profile["min_rr"]):
        reject("liquidity_barrier_room")
        return None

    components = _score_components(trigger_last, direction, zone, sweep, barrier_rr, tuned_profile)
    raw_score = _sum_components(components)
    if raw_score < _directional_min_raw_score(_profile_name(tuned_profile), direction):
        reject("liquidity_score_floor")
        return None

    payload = {
        "direction": direction,
        "entry_price": breakout._round_price_dynamic(entry_price),
        "stop_loss": stop_loss,
        "take_profits": take_profits,
        "risk_atr": trade["risk_atr"],
        "target_model": TARGET_MODEL_NAME,
        "targets": trade["targets"],
        "score": raw_score,
        "raw_score": raw_score,
        "normalized_score": raw_score,
        "components": components,
        "raw_components": components,
        "normalized_components": components,
        "timeframes": ["15M", EXECUTION_TIMEFRAME],
        "setup_group": str(tuned_profile["name"]),
        "delivery_plan": str(tuned_profile["name"]),
        "filter_profile": _profile_name(tuned_profile),
        "atr_pct": round(atr_pct, 6),
        "atr_value": breakout._round_price_dynamic(atr_now),
        "score_profile": str(tuned_profile["name"]),
        "score_calibration": SCORE_CALIBRATION_VERSION,
        "send_mode": SEND_MODE_PENDING_ENTRY,
        "entry_model_price": breakout._round_price_dynamic(confirmation_close),
        "entry_sent_price": breakout._round_price_dynamic(current_reference_price),
        "setup_stage": SETUP_STAGE_WAITING_PULLBACK,
        "entry_model": ENTRY_MODEL_NAME,
        "execution_timeframe": EXECUTION_TIMEFRAME,
        "entry_zone": entry_zone,
        "setup_confirmed_at": trigger_last.get("close_time"),
        "preentry_distance_atr": round(preentry_distance_atr, 4),
        "bars_after_sweep": bars_after_sweep,
        "sweep_to_market_atr": round(sweep_to_market_atr, 4),
        "tp1_rr": round(publish_tp1_rr, 4),
        "higher_tf_context": {"liquidity_zone": level, "sweep_distance_atr": sweep.get("sweep_distance_atr")},
        "liquidity_zone": breakout._round_price_dynamic(level),
        "liquidity_sweep_distance_atr": sweep.get("sweep_distance_atr"),
        "liquidity_sweep_extreme_price": sweep.get("extreme_price"),
        "liquidity_sweep_at": df_5m.loc[sweep["index"]].get("close_time"),
        "strategy_runtime": {
            "post_fill_invalidation": {
                "minutes": POST_FILL_INVALIDATION_MINUTES,
                "min_tp1_progress_pct": POST_FILL_MIN_TP1_PROGRESS_PCT,
                "reason": "liquidity_no_followthrough",
            }
        },
    }
    ranking = (raw_score, barrier_rr, float(zone.get("count") or 0.0))
    return payload, ranking

def mtf_strategies_by_plan(
    df_1h: pd.DataFrame,
    df_15m: pd.DataFrame,
    df_5m: Optional[pd.DataFrame] = None,
    reference_market_price: Optional[float] = None,
    debug_counts: Optional[Dict[str, int]] = None,
    observed_sweeps: Optional[Mapping] = None,
    on_sweep: Optional[Callable] = None,
) -> Dict[str, Dict]:
    """Evaluate the market for every configured signal profile.

    Currently only the PREMIUM profile exists (the PLUS plan was removed and
    FREE users receive the premium signal during their trial). The mapping
    keeps the return shape ``{profile_name: payload}`` used by the scanner.
    """
    closed_15m = _closed_15m_frame(df_15m)
    if len(closed_15m) < MIN_HISTORY_BARS:
        _record_reject(debug_counts, "liquidity_insufficient_history")
        return {}

    enriched_15m = add_indicators(closed_15m)
    if enriched_15m is None or enriched_15m.empty or not _indicators_ready(enriched_15m.iloc[-1]):
        _record_reject(debug_counts, "liquidity_indicator_warmup")
        return {}

    closed_1h = _closed_15m_frame(df_1h) if df_1h is not None and not df_1h.empty else df_1h
    enriched_1h = add_indicators(closed_1h) if closed_1h is not None and not closed_1h.empty else closed_1h

    closed_5m = _closed_15m_frame(df_5m) if df_5m is not None and not df_5m.empty else df_5m
    enriched_5m = add_indicators(closed_5m) if closed_5m is not None and not closed_5m.empty else closed_5m

    results: Dict[str, Dict] = {}
    for profile in PROFILES:
        candidates: List[Tuple[Dict, Tuple[float, float, float]]] = []
        for direction in ("LONG", "SHORT"):
            result = _evaluate_direction_5m_execution(
                enriched_15m,
                enriched_1h,
                enriched_5m,
                direction,
                profile,
                current_market_price=reference_market_price,
                debug_counts=debug_counts,
                observed_sweep=(observed_sweeps or {}).get((_profile_name(profile), direction)),
                on_sweep=on_sweep,
            )
            if result is None and (enriched_5m is None or enriched_5m.empty):
                result = _evaluate_direction(
                    enriched_15m,
                    enriched_1h,
                    df_5m,
                    direction,
                    profile,
                    current_market_price=reference_market_price,
                    debug_counts=debug_counts,
                )
            if result is not None:
                candidates.append(result)

        if candidates:
            candidates.sort(key=lambda item: item[1], reverse=True)
            payload = candidates[0][0]
            results[str(profile["name"])] = payload

    return results


def mtf_strategy(
    df_1h: pd.DataFrame,
    df_15m: pd.DataFrame,
    df_5m: Optional[pd.DataFrame] = None,
    reference_market_price: Optional[float] = None,
    debug_counts: Optional[Dict[str, int]] = None,
) -> Optional[Dict]:
    """Backward-compatible single-result API.

    New scanner code uses :func:`mtf_strategies_by_plan`. Existing callers keep
    the historical priority order (now Premium only).
    """
    results = mtf_strategies_by_plan(
        df_1h,
        df_15m,
        df_5m,
        reference_market_price=reference_market_price,
        debug_counts=debug_counts,
    )
    for profile in PROFILES:
        candidate = results.get(str(profile["name"]))
        if candidate is not None:
            return candidate
    return None
