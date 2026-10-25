# app/regime.py
"""Detector de régimen de mercado.

Clasifica el mercado (referencia: BTCUSDT) en TENDENCIA ALCISTA, TENDENCIA
BAJISTA, RANGO, TRANSICIÓN o ALTA VOLATILIDAD y lo enriquece con contexto:
fuerza (ADX), volatilidad (ATR%), Bollinger, volumen relativo, amplitud del
mercado, funding/open interest y sesión de trading.

Este módulo es puro (sin Telegram, Mongo ni red): recibe DataFrames y devuelve
un snapshot. El scanner lo refresca periódicamente y el botón del bot sólo lee
la caché en memoria, por lo que la respuesta es instantánea.
"""

import copy
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

REGIME_UP = "UP"
REGIME_DOWN = "DOWN"
REGIME_RANGE = "RANGE"
REGIME_TRANSITION = "TRANSITION"
REGIME_VOLATILE = "VOLATILE"

TIMEFRAME_WEIGHTS = {"15m": 0.2, "1h": 0.5, "4h": 0.3}
TIMEFRAME_LABELS = {"15m": "15M", "1h": "1H", "4h": "4H"}

MIN_BARS = 60
ADX_PERIOD = 14
ADX_RANGE_MAX = 20.0      # por debajo: mercado lateral
ADX_TREND_MIN = 25.0      # por encima: tendencia establecida
ADX_STRONG = 40.0
TREND_SCORE_THRESHOLD = 0.30
VOLATILITY_EXTREME_PCT = 90.0
FUNDING_CROWDED = 0.0005  # 0.05 %


# ======================================================
# UTILIDADES
# ======================================================
def _closed_frame(df: Optional[pd.DataFrame]) -> pd.DataFrame:
    """Devuelve sólo velas cerradas (descarta la vela abierta actual)."""
    if df is None or df.empty:
        return pd.DataFrame()
    frame = df.copy()
    if "close_time" in frame.columns:
        close_time = frame["close_time"]
        if pd.api.types.is_numeric_dtype(close_time):
            close_time = pd.to_datetime(close_time, unit="ms", utc=True, errors="coerce")
        else:
            close_time = pd.to_datetime(close_time, utc=True, errors="coerce")
        valid = close_time.notna()
        if valid.any():
            closed = frame.loc[valid & (close_time <= pd.Timestamp.now(tz="UTC"))]
            if not closed.empty:
                return closed.reset_index(drop=True)
    return frame.reset_index(drop=True)


def _wilder(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def _directional_indicators(frame: pd.DataFrame, period: int = ADX_PERIOD):
    high = frame["high"].astype(float)
    low = frame["low"].astype(float)
    close = frame["close"].astype(float)

    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=frame.index
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=frame.index
    )
    prev_close = close.shift(1)
    true_range = pd.concat(
        [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)

    atr = _wilder(true_range, period)
    safe_atr = atr.replace(0, np.nan)
    plus_di = 100.0 * _wilder(plus_dm, period) / safe_atr
    minus_di = 100.0 * _wilder(minus_dm, period) / safe_atr
    di_sum = (plus_di + minus_di).replace(0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / di_sum
    adx = dx.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    return adx, plus_di, minus_di, atr


def _finite(*values) -> bool:
    try:
        return all(np.isfinite(float(v)) for v in values)
    except (TypeError, ValueError):
        return False


# ======================================================
# ANÁLISIS POR TEMPORALIDAD
# ======================================================
def analyze_timeframe(df: Optional[pd.DataFrame]) -> Optional[Dict]:
    frame = _closed_frame(df)
    if len(frame) < MIN_BARS:
        return None

    close = frame["close"].astype(float)
    volume = frame["volume"].astype(float)
    ema20 = close.ewm(span=20, adjust=False, min_periods=20).mean()
    ema50 = close.ewm(span=50, adjust=False, min_periods=50).mean()
    ema200 = close.ewm(span=200, adjust=False, min_periods=100).mean()
    adx, plus_di, minus_di, atr = _directional_indicators(frame)
    atr_pct = atr / close.replace(0, np.nan)
    basis = close.rolling(20, min_periods=20).mean()
    band_width = 4.0 * close.rolling(20, min_periods=20).std() / basis.replace(0, np.nan)

    last = len(frame) - 1
    c = float(close.iloc[last])
    e20 = float(ema20.iloc[last])
    e50 = float(ema50.iloc[last])
    adx_now = float(adx.iloc[last])
    pdi = float(plus_di.iloc[last])
    mdi = float(minus_di.iloc[last])
    atr_pct_now = float(atr_pct.iloc[last])
    if not _finite(c, e20, e50, adx_now, pdi, mdi, atr_pct_now):
        return None

    window = atr_pct.dropna().tail(100)
    vol_percentile = float((window <= atr_pct_now).mean() * 100.0) if len(window) else 50.0

    width_now = float(band_width.iloc[last])
    width_prev = float(band_width.iloc[last - 5]) if last >= 5 else float("nan")
    if _finite(width_now, width_prev) and width_prev > 0:
        ratio = width_now / width_prev
        bandwidth_trend = "expanding" if ratio > 1.10 else "contracting" if ratio < 0.90 else "stable"
    else:
        bandwidth_trend = "stable"

    prev_volume_avg = float(volume.iloc[-21:-1].mean()) if len(volume) > 21 else 0.0
    rel_volume = float(volume.iloc[-1] / prev_volume_avg) if prev_volume_avg > 0 else 1.0

    slope_base = float(ema50.iloc[last - 5]) if last >= 5 else float("nan")
    ema50_slope_pct = (e50 / slope_base - 1.0) * 100.0 if _finite(slope_base) and slope_base else 0.0

    e200_raw = float(ema200.iloc[last])
    above_ema200 = (c > e200_raw) if _finite(e200_raw) else None

    votes = int(pdi > mdi) + int(e20 > e50) + int(c > e50)
    direction = REGIME_UP if votes >= 2 else REGIME_DOWN

    if adx_now < ADX_RANGE_MAX:
        regime = REGIME_RANGE
    elif adx_now < ADX_TREND_MIN:
        regime = direction if votes in (0, 3) else REGIME_TRANSITION
    else:
        regime = direction

    if adx_now >= ADX_STRONG:
        strength = "fuerte"
    elif adx_now >= 30:
        strength = "clara"
    elif adx_now >= ADX_TREND_MIN:
        strength = "moderada"
    elif adx_now >= ADX_RANGE_MAX:
        strength = "débil"
    else:
        strength = "sin tendencia"

    return {
        "regime": regime,
        "strength": strength,
        "adx": round(adx_now, 1),
        "plus_di": round(pdi, 1),
        "minus_di": round(mdi, 1),
        "close": c,
        "ema20": e20,
        "ema50": e50,
        "above_ema200": above_ema200,
        "ema50_slope_pct": round(ema50_slope_pct, 3),
        "atr_pct": round(atr_pct_now * 100.0, 3),
        "vol_percentile": round(vol_percentile, 0),
        "bandwidth_trend": bandwidth_trend,
        "rel_volume": round(rel_volume, 2),
    }


# ======================================================
# AMPLITUD DEL MERCADO (la aporta el scanner)
# ======================================================
def symbol_trend_state(df_1h: Optional[pd.DataFrame]) -> Optional[Dict[str, bool]]:
    """Estado barato de un símbolo en 1H para calcular la amplitud del mercado."""
    frame = _closed_frame(df_1h)
    if len(frame) < 55:
        return None
    close = frame["close"].astype(float)
    ema20 = float(close.ewm(span=20, adjust=False, min_periods=20).mean().iloc[-1])
    ema50 = float(close.ewm(span=50, adjust=False, min_periods=50).mean().iloc[-1])
    price = float(close.iloc[-1])
    if not _finite(price, ema20, ema50):
        return None
    return {"above_ema50": price > ema50, "bullish": ema20 > ema50}


def summarize_breadth(states: Iterable[Optional[Dict[str, bool]]]) -> Optional[Dict]:
    valid = [s for s in states if s]
    total = len(valid)
    if total < 10:
        return None
    above = sum(1 for s in valid if s["above_ema50"])
    bull = sum(1 for s in valid if s["bullish"])
    return {
        "total": total,
        "above_ema50_pct": round(100.0 * above / total, 0),
        "bullish_pct": round(100.0 * bull / total, 0),
        "bearish_pct": round(100.0 * (total - bull) / total, 0),
    }


# ======================================================
# SESIÓN DE TRADING
# ======================================================
def trading_session(now: Optional[datetime] = None) -> Dict[str, str]:
    now = now or datetime.now(timezone.utc)
    hour = now.astimezone(timezone.utc).hour
    if 0 <= hour < 7:
        return {"name": "Asia", "liquidity": "liquidez media"}
    if 7 <= hour < 13:
        return {"name": "Londres", "liquidity": "liquidez alta"}
    if 13 <= hour < 16:
        return {"name": "Londres + Nueva York", "liquidity": "máxima liquidez"}
    if 16 <= hour < 21:
        return {"name": "Nueva York", "liquidity": "liquidez alta"}
    return {"name": "Cierre de NY / pre-Asia", "liquidity": "liquidez baja"}


# ======================================================
# RÉGIMEN GLOBAL
# ======================================================
def _tf_sign(regime: str) -> int:
    return 1 if regime == REGIME_UP else -1 if regime == REGIME_DOWN else 0


def _reference_context(df_1h: Optional[pd.DataFrame], tf1h: Dict) -> Dict:
    frame = _closed_frame(df_1h)
    context = {
        "price": tf1h["close"],
        "above_ema20": tf1h["close"] > tf1h["ema20"],
        "above_ema50": tf1h["close"] > tf1h["ema50"],
        "above_ema200": tf1h.get("above_ema200"),
        "range_24h_pos_pct": None,
        "change_24h_pct": None,
    }
    tail = frame.tail(24)
    if len(tail) >= 12:
        low = float(tail["low"].astype(float).min())
        high = float(tail["high"].astype(float).max())
        if high > low:
            context["range_24h_pos_pct"] = round(100.0 * (tf1h["close"] - low) / (high - low), 0)
    if len(frame) >= 25:
        base = float(frame["close"].astype(float).iloc[-25])
        if base > 0:
            context["change_24h_pct"] = round((tf1h["close"] / base - 1.0) * 100.0, 2)
    return context


def _bot_guidance(regime: str, vol_label: str, breadth: Optional[Dict]) -> List[str]:
    lines: List[str] = []
    if regime == REGIME_UP:
        lines += [
            "✅ LONG: barridos de mínimos con recuperación van a favor de la tendencia",
            "⚠️ SHORT: operar sólo con confirmación fuerte (va contra tendencia)",
        ]
    elif regime == REGIME_DOWN:
        lines += [
            "✅ SHORT: barridos de máximos con rechazo van a favor de la tendencia",
            "⚠️ LONG: operar sólo con confirmación fuerte (va contra tendencia)",
        ]
    elif regime == REGIME_RANGE:
        lines += [
            "↔️ Los barridos en ambos extremos del rango suelen revertir",
            "🎯 Preferir objetivos conservadores; evitar perseguir rupturas",
        ]
    elif regime == REGIME_VOLATILE:
        lines += [
            "⚡ Movimientos amplios sin dirección: abundan las falsas rupturas",
            "🛡 Exigir confirmación y reducir tamaño de posición",
        ]
    else:
        lines += [
            "⏳ Sin dirección clara entre temporalidades: esperar confirmación",
            "🛡 Priorizar calidad sobre cantidad de señales",
        ]

    if vol_label in ("alta", "extrema"):
        lines.append("📏 Volatilidad elevada: dar más margen al SL y bajar el apalancamiento")
    elif vol_label == "baja":
        lines.append("📏 Volatilidad baja: los movimientos pueden ser cortos; ajustar expectativas de TP")

    if breadth:
        if regime == REGIME_UP and breadth["bullish_pct"] < 45:
            lines.append("🔎 Divergencia: BTC sube pero la mayoría de pares sigue débil")
        elif regime == REGIME_DOWN and breadth["bullish_pct"] > 55:
            lines.append("🔎 Divergencia: BTC cae pero la mayoría de pares aguanta")
    return lines


def volatility_label(percentile: float) -> str:
    if percentile >= VOLATILITY_EXTREME_PCT:
        return "extrema"
    if percentile >= 70:
        return "alta"
    if percentile >= 30:
        return "normal"
    return "baja"


def compute_market_regime(
    frames: Dict[str, Optional[pd.DataFrame]],
    symbol: str = "BTCUSDT",
    breadth: Optional[Dict] = None,
    funding_rate: Optional[float] = None,
    oi_change_24h_pct: Optional[float] = None,
    now: Optional[datetime] = None,
) -> Optional[Dict]:
    """Calcula el snapshot completo. Requiere al menos la temporalidad 1h."""
    analyses = {}
    for tf in TIMEFRAME_WEIGHTS:
        result = analyze_timeframe(frames.get(tf))
        if result is not None:
            analyses[tf] = result
    if "1h" not in analyses:
        return None

    total_weight = sum(TIMEFRAME_WEIGHTS[tf] for tf in analyses)
    score = sum(
        TIMEFRAME_WEIGHTS[tf] * _tf_sign(a["regime"]) * min(a["adx"] / ADX_STRONG, 1.0)
        for tf, a in analyses.items()
    ) / total_weight

    tf1h = analyses["1h"]
    if score >= TREND_SCORE_THRESHOLD:
        regime = REGIME_UP
    elif score <= -TREND_SCORE_THRESHOLD:
        regime = REGIME_DOWN
    elif tf1h["regime"] == REGIME_TRANSITION:
        regime = REGIME_TRANSITION
    elif tf1h["vol_percentile"] >= VOLATILITY_EXTREME_PCT:
        regime = REGIME_VOLATILE
    else:
        regime = REGIME_RANGE

    def matches(tf_regime: str) -> bool:
        if regime in (REGIME_UP, REGIME_DOWN):
            return tf_regime == regime
        return tf_regime in (REGIME_RANGE, REGIME_TRANSITION)

    agreement = sum(TIMEFRAME_WEIGHTS[tf] for tf, a in analyses.items() if matches(a["regime"])) / total_weight
    if regime in (REGIME_UP, REGIME_DOWN):
        strength = min(abs(score) / 0.6, 1.0)
    elif regime == REGIME_RANGE:
        strength = min(max((ADX_TREND_MIN - tf1h["adx"]) / 15.0, 0.0), 1.0)
    else:
        strength = 0.5
    confidence = int(round(100 * (0.6 * agreement + 0.4 * strength)))
    confidence = max(20, min(95, confidence))

    vol_label = volatility_label(tf1h["vol_percentile"])
    now = now or datetime.now(timezone.utc)

    return {
        "ts": time.time(),
        "symbol": symbol,
        "regime": regime,
        "confidence": confidence,
        "score": round(score, 3),
        "timeframes": analyses,
        "volatility_label": vol_label,
        "reference": _reference_context(frames.get("1h"), tf1h),
        "breadth": breadth,
        "funding_rate": funding_rate,
        "oi_change_24h_pct": oi_change_24h_pct,
        "session": trading_session(now),
        "guidance": _bot_guidance(regime, vol_label, breadth),
    }


# ======================================================
# CACHÉ EN MEMORIA
# ======================================================
_lock = threading.Lock()
_snapshot: Optional[Dict] = None


def set_snapshot(snapshot: Dict) -> None:
    global _snapshot
    with _lock:
        _snapshot = snapshot


def get_snapshot() -> Optional[Dict]:
    with _lock:
        return copy.deepcopy(_snapshot) if _snapshot is not None else None


def snapshot_age_seconds() -> Optional[float]:
    with _lock:
        return (time.time() - _snapshot["ts"]) if _snapshot is not None else None


# ======================================================
# FORMATO DEL MENSAJE DE TELEGRAM
# ======================================================
REGIME_TITLES = {
    REGIME_UP: "📈 TENDENCIA ALCISTA",
    REGIME_DOWN: "📉 TENDENCIA BAJISTA",
    REGIME_RANGE: "↔️ RANGO LATERAL",
    REGIME_TRANSITION: "🔀 TRANSICIÓN (sin dirección clara)",
    REGIME_VOLATILE: "⚡ ALTA VOLATILIDAD SIN DIRECCIÓN",
}


def _bar(percent: float, width: int = 10) -> str:
    filled = max(0, min(width, int(round(percent / 100.0 * width))))
    return "█" * filled + "░" * (width - filled)


def _tf_line(tf: str, analysis: Dict) -> str:
    regime = analysis["regime"]
    if regime == REGIME_UP:
        text = f"Alcista ↗ ({analysis['strength']})"
    elif regime == REGIME_DOWN:
        text = f"Bajista ↘ ({analysis['strength']})"
    elif regime == REGIME_RANGE:
        text = "Rango ↔"
    else:
        text = "Transición 🔀"
    return f"• {TIMEFRAME_LABELS[tf]}: {text}"


def _adx_meaning(adx: float) -> str:
    if adx >= ADX_STRONG:
        return "tendencia muy fuerte"
    if adx >= ADX_TREND_MIN:
        return "tendencia clara"
    if adx >= ADX_RANGE_MAX:
        return "tendencia débil / transición"
    return "sin tendencia (rango)"


def _volume_text(rel: float) -> str:
    if rel > 1.1:
        return "por encima del promedio"
    if rel < 0.9:
        return "por debajo del promedio"
    return "en el promedio"


def _bandwidth_text(trend: str) -> str:
    return {"expanding": "expandiéndose", "contracting": "contrayéndose"}.get(trend, "estable")


def format_regime_message(snapshot: Dict) -> str:
    age_min = max(0, int((time.time() - snapshot["ts"]) // 60))
    updated = "justo ahora" if age_min == 0 else f"hace {age_min} min"
    tf1h = snapshot["timeframes"]["1h"]
    ref = snapshot["reference"]

    lines = [
        "🧭 RÉGIMEN DEL MERCADO",
        f"Actualizado {updated} · base {snapshot['symbol']}",
        "",
        f"📊 Régimen actual: {REGIME_TITLES[snapshot['regime']]}",
        f"Confianza: {snapshot['confidence']}% {_bar(snapshot['confidence'])}",
        "",
        "⏱ Por temporalidad",
    ]
    for tf in ("15m", "1h", "4h"):
        if tf in snapshot["timeframes"]:
            lines.append(_tf_line(tf, snapshot["timeframes"][tf]))

    lines += [
        "",
        f"📈 Fuerza (ADX 1H): {tf1h['adx']:.0f} → {_adx_meaning(tf1h['adx'])}",
        f"🌊 Volatilidad (ATR% 1H): {tf1h['atr_pct']:.2f}% → {snapshot['volatility_label']} "
        f"(percentil {tf1h['vol_percentile']:.0f})",
        f"📏 Ancho Bollinger: {_bandwidth_text(tf1h['bandwidth_trend'])}",
        f"🔊 Volumen relativo: {tf1h['rel_volume']:.1f}x ({_volume_text(tf1h['rel_volume'])})",
        "",
        f"🪙 {snapshot['symbol'].replace('USDT', '')} (referencia)",
        f"Precio vs EMA20/50: {'por encima' if ref['above_ema20'] else 'por debajo'} de EMA20 · "
        f"{'por encima' if ref['above_ema50'] else 'por debajo'} de EMA50",
    ]
    if ref.get("change_24h_pct") is not None:
        lines.append(f"Cambio 24h: {ref['change_24h_pct']:+.2f}%")
    if ref.get("range_24h_pos_pct") is not None:
        lines.append(f"Posición en rango 24h: {ref['range_24h_pos_pct']:.0f}% (0 = mínimo, 100 = máximo)")

    breadth = snapshot.get("breadth")
    if breadth:
        lines += [
            "",
            f"🌡 Amplitud del mercado ({breadth['total']} pares escaneados)",
            f"• {breadth['above_ema50_pct']:.0f}% sobre su EMA50 en 1H",
            f"• {breadth['bullish_pct']:.0f}% alcistas / {breadth['bearish_pct']:.0f}% bajistas",
        ]

    funding = snapshot.get("funding_rate")
    oi_change = snapshot.get("oi_change_24h_pct")
    if funding is not None or oi_change is not None:
        lines += ["", "💱 Derivados"]
        if funding is not None:
            if funding > FUNDING_CROWDED:
                note = "longs muy apalancados"
            elif funding < -FUNDING_CROWDED:
                note = "shorts muy apalancados"
            else:
                note = "neutral"
            lines.append(f"• Funding: {funding * 100:+.4f}% ({note})")
        if oi_change is not None:
            lines.append(f"• Open interest 24h: {oi_change:+.1f}%")

    lines += ["", "⚡ Qué significa para el bot"] + list(snapshot.get("guidance", []))

    session = snapshot["session"]
    lines += [
        "",
        f"🕒 Sesión: {session['name']} ({session['liquidity']})",
        "",
        "ℹ️ Información orientativa, no es una recomendación de inversión.",
    ]
    return "\n".join(lines)
