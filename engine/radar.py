# app/radar.py
"""Radar de oportunidades.

Lista priorizada de oportunidades *en formación*, es decir, lo que está cerca de
convertirse en señal:

- Setups activos (PENDING/TOUCHED) del plan del usuario, con score y desglose.
- Barridos de liquidez detectados que aún no tienen confirmación.
- Pares en vigilancia: precio cerca de una zona de liquidez de 15M.
- Movimientos inusuales: variación fuerte en 1h con volumen anómalo.

Este módulo es puro (sin Telegram, Mongo ni red). El scanner calcula un item por
símbolo con `analyze_symbol` durante su ciclo normal (sin descargas extra) y
construye un snapshot en memoria con `build_radar_snapshot`. El botón del bot
solo lee ese snapshot y lo combina con los setups del usuario.
"""

import copy
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

import numpy as np
import pandas as pd

from engine.regime import REGIME_DOWN, REGIME_RANGE, REGIME_TRANSITION, REGIME_UP, REGIME_VOLATILE
from engine.liquidity_strategy import (
    FREE_PROFILE,
    MIN_HISTORY_BARS,
    _closed_15m_frame,
    _indicators_ready,
    _select_liquidity_zone,
    add_indicators,
)

logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return float(default)


RADAR_WATCH_MAX_ATR = _env_float("RADAR_WATCH_MAX_ATR", 1.2)   # distancia máx. a la zona (en ATR 15M)
RADAR_MOVE_PCT = _env_float("RADAR_MOVE_PCT", 3.0)             # variación mínima en 1h (%)
RADAR_MOVE_VOL = _env_float("RADAR_MOVE_VOL", 2.5)             # volumen mínimo vs. promedio (x)
FUNDING_NOTICE = 0.0003                                        # 0.03 %
SWEEP_RETENTION_MIN = 20                                       # igual que sweep_tracker.RETENTION_MINUTES
STORE_WATCH = 15
STORE_MOVERS = 5
MAX_TELEGRAM_CHARS = 3900

COMPONENT_LABELS = [
    ("liquidity_zone", "liquidez", 18),
    ("minimum_sweep", "barrido", 18),
    ("recovery_close", "recuperación", 16),
    ("confirmation_volume", "volumen", 14),
    ("close_position", "cierre", 12),
    ("ema_reclaim", "EMA", 10),
    ("barrier_room_rr", "espacio", 12),
]


# ======================================================
# UTILIDADES
# ======================================================
def _ok(*values) -> bool:
    try:
        return all(np.isfinite(float(v)) for v in values)
    except (TypeError, ValueError):
        return False


def _to_ts(value) -> Optional[float]:
    """Datetime (naive = UTC) -> epoch seconds."""
    if value is None:
        return None
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def _fmt_price(value) -> str:
    value = float(value)
    if abs(value) >= 1000:
        return f"{value:,.2f}"
    if abs(value) >= 1:
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _trend_1h(df_1h: Optional[pd.DataFrame]) -> Optional[str]:
    if df_1h is None or df_1h.empty:
        return None
    frame = _closed_15m_frame(df_1h)
    if frame is None or len(frame) < 55:
        return None
    close = frame["close"].astype(float)
    ema20 = float(close.ewm(span=20, adjust=False, min_periods=20).mean().iloc[-1])
    ema50 = float(close.ewm(span=50, adjust=False, min_periods=50).mean().iloc[-1])
    price = float(close.iloc[-1])
    if not _ok(price, ema20, ema50):
        return None
    if price > ema50 and ema20 > ema50:
        return "UP"
    if price < ema50 and ema20 < ema50:
        return "DOWN"
    return "MIXED"


def _movement(df_5m: Optional[pd.DataFrame], price: float) -> Optional[Dict[str, float]]:
    """Variación de la última hora y volumen de la hora vs. su promedio reciente."""
    if df_5m is None or len(df_5m) < 40:
        return None
    close = df_5m["close"].astype(float)
    volume = df_5m["volume"].astype(float)
    base_price = float(close.iloc[-13])
    baseline = float(volume.iloc[:-12].tail(96).mean())
    if not _ok(base_price, baseline) or base_price <= 0 or baseline <= 0:
        return None
    last_hour = float(volume.iloc[-12:].sum())
    return {
        "change_1h_pct": round((price / base_price - 1.0) * 100.0, 2),
        "vol_ratio": round(last_hour / (baseline * 12.0), 2),
    }


def trend_alignment(direction: str, trend: Optional[str]) -> str:
    if trend == "UP":
        return "favor" if direction == "LONG" else "contra"
    if trend == "DOWN":
        return "favor" if direction == "SHORT" else "contra"
    return "neutral"


# ======================================================
# ANÁLISIS POR SÍMBOLO (se ejecuta dentro del ciclo del scanner)
# ======================================================
def analyze_symbol(
    symbol: str,
    df_1h: Optional[pd.DataFrame],
    df_15m: Optional[pd.DataFrame],
    df_5m: Optional[pd.DataFrame],
    watch_max_atr: Optional[float] = None,
) -> Optional[Dict]:
    watch_max_atr = RADAR_WATCH_MAX_ATR if watch_max_atr is None else watch_max_atr
    closed_15m = _closed_15m_frame(df_15m)
    if closed_15m is None or len(closed_15m) < MIN_HISTORY_BARS:
        return None
    enriched = add_indicators(closed_15m)
    if enriched is None or enriched.empty or not _indicators_ready(enriched.iloc[-1]):
        return None

    last = enriched.iloc[-1]
    atr = float(last["atr"])
    rel_volume = float(last["rel_volume"])
    if df_5m is not None and not df_5m.empty:
        price = float(df_5m.iloc[-1]["close"])
    else:
        price = float(last["close"])
    if not _ok(price, atr, rel_volume) or price <= 0 or atr <= 0:
        return None

    trend = _trend_1h(df_1h)
    watch: List[Dict] = []
    for direction in ("LONG", "SHORT"):
        zone = _select_liquidity_zone(enriched, direction, FREE_PROFILE)
        if not zone:
            continue
        level = float(zone["price"])
        distance = (price - level) if direction == "LONG" else (level - price)
        if distance < 0:
            continue  # el precio ya cruzó el nivel: lo cubren los barridos observados
        dist_atr = distance / atr
        if dist_atr > watch_max_atr:
            continue
        count = int(zone.get("count") or 1)
        alignment = trend_alignment(direction, trend)
        score = (
            40.0 * (1.0 - dist_atr / watch_max_atr)
            + 20.0 * min(count, 3) / 3.0
            + {"favor": 20.0, "neutral": 10.0, "contra": 0.0}[alignment]
            + 20.0 * min(rel_volume / 1.5, 1.0)
        )
        watch.append({
            "symbol": symbol,
            "direction": direction,
            "level": level,
            "distance_pct": round(distance / price * 100.0, 2),
            "distance_atr": round(dist_atr, 2),
            "zone_count": count,
            "rel_volume": round(rel_volume, 2),
            "trend_1h": trend,
            "alignment": alignment,
            "watch_score": int(round(max(0.0, min(100.0, score)))),
        })

    return {
        "symbol": symbol,
        "price": price,
        "atr_pct": round(atr / price * 100.0, 3),
        "rel_volume": round(rel_volume, 2),
        "trend_1h": trend,
        "watch": watch,
        "move": _movement(df_5m, price),
    }


# ======================================================
# SNAPSHOT GLOBAL
# ======================================================
def build_radar_snapshot(
    items: Iterable[Optional[Dict]],
    sweeps: Iterable[Dict],
    funding: Optional[Dict[str, float]] = None,
    now: Optional[datetime] = None,
) -> Dict:
    now_ts = (now.timestamp() if now else time.time())
    valid = [item for item in items if item]
    by_symbol = {item["symbol"]: item for item in valid}

    watch = [entry for item in valid for entry in item["watch"]]
    watch.sort(key=lambda e: e["watch_score"], reverse=True)

    movers = []
    for item in valid:
        move = item.get("move")
        if not move:
            continue
        if abs(move["change_1h_pct"]) >= RADAR_MOVE_PCT and move["vol_ratio"] >= RADAR_MOVE_VOL:
            movers.append({
                "symbol": item["symbol"],
                "change_1h_pct": move["change_1h_pct"],
                "vol_ratio": move["vol_ratio"],
            })
    movers.sort(key=lambda m: m["vol_ratio"], reverse=True)

    latest: Dict[tuple, Dict] = {}
    for obs in sweeps:
        key = (obs.get("symbol"), obs.get("direction"))
        event_ts = _to_ts(obs.get("event_at"))
        if event_ts is None:
            continue
        if key not in latest or event_ts > latest[key]["event_ts"]:
            latest[key] = {**obs, "event_ts": event_ts}

    sweeps_out = []
    for (symbol, direction), obs in latest.items():
        age_min = (now_ts - obs["event_ts"]) / 60.0
        if age_min > SWEEP_RETENTION_MIN or age_min < -1:
            continue
        info = by_symbol.get(symbol) or {}
        price = info.get("price")
        extreme = float(obs["extreme_price"])
        # Si el precio ya perdió el extremo del barrido, el barrido quedó invalidado.
        if price is not None and ((direction == "LONG" and price <= extreme) or (direction == "SHORT" and price >= extreme)):
            continue
        sweeps_out.append({
            "symbol": symbol,
            "direction": direction,
            "level": float(obs["level"]),
            "extreme_price": extreme,
            "wick_ratio": float(obs.get("wick_ratio") or 0.0),
            "sweep_distance_atr": float(obs.get("sweep_distance_atr") or 0.0),
            "event_ts": obs["event_ts"],
            "price": price,
            "rel_volume": info.get("rel_volume"),
            "trend_1h": info.get("trend_1h"),
            "atr_pct": info.get("atr_pct"),
        })
    sweeps_out.sort(key=lambda s: s["event_ts"], reverse=True)

    return {
        "ts": now_ts,
        "scanned": len(valid),
        "prices": {item["symbol"]: item["price"] for item in valid},
        "funding": dict(funding or {}),
        "watch": watch[:STORE_WATCH],
        "watch_total": len(watch),
        "sweeps": sweeps_out,
        "movers": movers[:STORE_MOVERS],
    }


# ======================================================
# CACHÉ EN MEMORIA
# ======================================================
_lock = threading.Lock()
_snapshot: Optional[Dict] = None


def set_radar_snapshot(snapshot: Dict) -> None:
    global _snapshot
    with _lock:
        _snapshot = snapshot


def get_radar_snapshot() -> Optional[Dict]:
    with _lock:
        return copy.deepcopy(_snapshot) if _snapshot is not None else None


# ======================================================
# ALINEACIÓN CON EL RÉGIMEN
# ======================================================
def alignment_for(direction: str, regime_snapshot: Optional[Dict], trend_1h: Optional[str] = None) -> Dict[str, str]:
    """Combina régimen de BTC y 1H del par. Devuelve {status, text}."""
    code = regime_snapshot.get("regime") if regime_snapshot else None
    if code in (REGIME_UP, REGIME_DOWN):
        favor = (direction == "LONG") == (code == REGIME_UP)
        word = "alcista" if code == REGIME_UP else "bajista"
        text = f"A favor del régimen {word}" if favor else f"Contra el régimen {word}: exige confirmación fuerte"
        status = "favor" if favor else "contra"
    elif trend_1h in ("UP", "DOWN"):
        status = trend_alignment(direction, trend_1h)
        word = "alcista" if trend_1h == "UP" else "bajista"
        text = f"A favor del 1H del par ({word})" if status == "favor" else f"Contra el 1H del par ({word}): exige confirmación"
        return {"status": status, "text": text}
    elif code == REGIME_RANGE:
        return {"status": "neutral", "text": "Mercado en rango: los barridos suelen revertir"}
    elif code in (REGIME_TRANSITION, REGIME_VOLATILE):
        return {"status": "neutral", "text": "Régimen sin dirección clara: esperar confirmación"}
    else:
        return {"status": "neutral", "text": ""}

    if trend_1h in ("UP", "DOWN"):
        pair = "alcista" if trend_1h == "UP" else "bajista"
        text += f" · 1H del par {pair}"
    return {"status": status, "text": text}


def sweep_score(sweep: Dict, alignment_status: str, now_ts: float) -> int:
    quality = min(sweep["sweep_distance_atr"] / 0.5, 1.0) * 15.0 + min(sweep["wick_ratio"] / 0.6, 1.0) * 20.0
    age_min = max(0.0, (now_ts - sweep["event_ts"]) / 60.0)
    recency = max(0.0, 1.0 - age_min / SWEEP_RETENTION_MIN) * 20.0
    align = {"favor": 25.0, "neutral": 12.0, "contra": 0.0}[alignment_status]
    volume = min(float(sweep.get("rel_volume") or 0.0) / 1.5, 1.0) * 20.0
    return int(round(max(0.0, min(100.0, quality + recency + align + volume))))


# ======================================================
# FORMATO DEL MENSAJE
# ======================================================
MEDALS = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
REGIME_HEADER = {
    REGIME_UP: "📈 alcista ({c}%) → LONG a favor",
    REGIME_DOWN: "📉 bajista ({c}%) → SHORT a favor",
    REGIME_RANGE: "↔️ rango ({c}%) → los barridos suelen revertir",
    REGIME_TRANSITION: "🔀 transición ({c}%) → sin dirección clara",
    REGIME_VOLATILE: "⚡ alta volatilidad ({c}%) → sin dirección clara",
}


def _plural(n: int, singular: str, plural: str) -> str:
    return f"{n} {singular if n == 1 else plural}"


def _ago(minutes: float) -> str:
    minutes = int(max(0, minutes))
    return "hace menos de 1 min" if minutes < 1 else f"hace {minutes} min"


def _funding_warning(direction: str, funding: Optional[float]) -> Optional[str]:
    if funding is None:
        return None
    if direction == "LONG" and funding >= FUNDING_NOTICE:
        return f"⚠️ Funding {funding * 100:+.3f}%: longs cargados"
    if direction == "SHORT" and funding <= -FUNDING_NOTICE:
        return f"⚠️ Funding {funding * 100:+.3f}%: shorts cargados"
    return None


def _rr(entry: float, stop: float, target: float) -> Optional[float]:
    risk = abs(entry - stop)
    return abs(target - entry) / risk if risk > 0 else None


def _render_setup(medal: str, setup: Dict, snapshot: Dict, regime: Optional[Dict], now_ts: float) -> str:
    symbol, direction = setup["symbol"], setup["direction"]
    score = setup.get("score")
    head = f"{medal} {symbol} · {direction}"
    if score is not None:
        head += f" · Score {float(score):.0f}/100"
    lines = [head]

    if str(setup.get("status")).upper() == "TOUCHED":
        touched = _to_ts(setup.get("touched_at"))
        when = f" {_ago((now_ts - touched) / 60)}" if touched else ""
        lines.append(f"🎯 Zona tocada{when}")
    else:
        lines.append("✅ Setup confirmado, esperando retroceso")

    zone = setup.get("entry_zone") or {}
    entry = float(setup.get("entry_price") or 0.0)
    if zone.get("low") is not None and zone.get("high") is not None:
        low, high = float(zone["low"]), float(zone["high"])
        price = snapshot["prices"].get(symbol) or setup.get("entry_sent_price")
        text = f"💰 Entrada {_fmt_price(low)}–{_fmt_price(high)}"
        if price:
            price = float(price)
            if low <= price <= high:
                text += f" (precio {_fmt_price(price)}, dentro de la zona)"
            else:
                edge = high if price > high else low
                text += f" (precio {_fmt_price(price)}, a {abs(price - edge) / price * 100:.1f}% de la zona)"
        lines.append(text)

    stop = setup.get("stop_loss")
    tps = [float(x) for x in (setup.get("take_profits") or [])]
    if stop and tps and entry:
        text = f"🛡 SL {_fmt_price(stop)} · 🎯 TP1 {_fmt_price(tps[0])}"
        rr1 = _rr(entry, float(stop), tps[0])
        if rr1:
            text += f" ({rr1:.1f}R)"
        if len(tps) > 1:
            text += f" · TP2 {_fmt_price(tps[1])}"
            rr2 = _rr(entry, float(stop), tps[1])
            if rr2:
                text += f" ({rr2:.1f}R)"
        lines.append(text)

    if setup.get("liquidity_sweep_extreme_price") is not None:
        text = f"🧲 Barrido hasta {_fmt_price(setup['liquidity_sweep_extreme_price'])}"
        if setup.get("liquidity_zone") is not None:
            text += f" (nivel de liquidez {_fmt_price(setup['liquidity_zone'])})"
        lines.append(text)

    comps = {c.get("name"): c for c in (setup.get("components") or []) if isinstance(c, dict)}
    extras = []
    vol = comps.get("confirmation_volume")
    if vol and vol.get("value"):
        extras.append(f"Volumen de confirmación {float(vol['value']):.1f}x")
    atr_value = setup.get("atr_value")
    if atr_value and entry:
        extras.append(f"ATR {float(atr_value) / entry * 100:.2f}%")
    if extras:
        lines.append("📊 " + " · ".join(extras))

    align = alignment_for(direction, regime, (snapshot["_trend"].get(symbol)))
    if align["text"]:
        lines.append("🧭 " + align["text"])

    expires = _to_ts(setup.get("entry_expires_at"))
    if str(setup.get("status")).upper() != "TOUCHED" and expires:
        minutes = (expires - now_ts) / 60
        lines.append("⏳ Expira en menos de 1 min" if minutes < 1 else f"⏳ Expira en {int(minutes)} min")

    warn = _funding_warning(direction, snapshot["funding"].get(symbol))
    if warn:
        lines.append(warn)

    parts = []
    for key, label, maximum in COMPONENT_LABELS:
        if key in comps:
            parts.append(f"{label} {float(comps[key].get('points') or 0):.0f}/{maximum}")
    if parts:
        lines.append("🧮 " + " · ".join(parts))
    return "\n".join(lines)


def _render_sweep(medal: str, sweep: Dict, score: int, regime: Optional[Dict], snapshot: Dict, now_ts: float) -> str:
    symbol, direction = sweep["symbol"], sweep["direction"]
    low_side = direction == "LONG"
    lines = [
        f"{medal} {symbol} · {direction} · Score radar {score}/100",
        f"👀 Barrido detectado {_ago((now_ts - sweep['event_ts']) / 60)}, falta confirmación",
        f"🧲 {'Mínimo' if low_side else 'Máximo'} {_fmt_price(sweep['level'])} barrido hasta "
        f"{_fmt_price(sweep['extreme_price'])} (mecha {sweep['wick_ratio'] * 100:.0f}%)",
        f"❓ Falta: cierre 5M {'sobre' if low_side else 'bajo'} {_fmt_price(sweep['level'])} "
        f"con desplazamiento {'alcista' if low_side else 'bajista'}",
    ]
    align = alignment_for(direction, regime, sweep.get("trend_1h"))
    if align["text"]:
        lines.append("🧭 " + align["text"])
    price = sweep.get("price")
    if price:
        pct = abs(price - sweep["extreme_price"]) / price * 100
        lines.append(f"📏 Invalidación a {pct:.1f}% (si el precio {'pierde' if low_side else 'supera'} {_fmt_price(sweep['extreme_price'])})")
    if sweep.get("rel_volume"):
        lines.append(f"📊 Volumen {float(sweep['rel_volume']):.1f}x")
    warn = _funding_warning(direction, snapshot["funding"].get(symbol))
    if warn:
        lines.append(warn)
    return "\n".join(lines)


def format_radar_message(
    snapshot: Dict,
    setups: Optional[List[Dict]] = None,
    regime: Optional[Dict] = None,
    now: Optional[datetime] = None,
) -> str:
    now_ts = now.timestamp() if now else time.time()
    setups = list(setups or [])
    snapshot = dict(snapshot)
    # 1H del par por símbolo, para la alineación de los setups confirmados.
    snapshot["_trend"] = {
        **{w["symbol"]: w.get("trend_1h") for w in snapshot.get("watch", [])},
        **{s["symbol"]: s.get("trend_1h") for s in snapshot.get("sweeps", [])},
    }

    setups.sort(key=lambda s: float(s.get("score") or 0.0), reverse=True)
    setup_keys = {(s["symbol"], s["direction"]) for s in setups}
    scored_sweeps = []
    for sweep in snapshot.get("sweeps", []):
        if (sweep["symbol"], sweep["direction"]) in setup_keys:
            continue  # ya es un setup confirmado
        align = alignment_for(sweep["direction"], regime, sweep.get("trend_1h"))
        scored_sweeps.append((sweep_score(sweep, align["status"], now_ts), sweep))
    scored_sweeps.sort(key=lambda pair: pair[0], reverse=True)

    age_min = max(0, int((now_ts - snapshot["ts"]) // 60))
    header = [
        "📡 RADAR DE OPORTUNIDADES",
        f"Actualizado {'justo ahora' if age_min == 0 else f'hace {age_min} min'} · {snapshot['scanned']} pares escaneados",
    ]
    if regime and regime.get("regime") in REGIME_HEADER:
        header.append("Régimen: " + REGIME_HEADER[regime["regime"]].format(c=regime.get("confidence", 0)))

    summary = [
        "",
        "🔎 Resumen",
        f"• {_plural(len(setups), 'setup activo', 'setups activos')} · "
        f"{_plural(len(scored_sweeps), 'barrido sin confirmar', 'barridos sin confirmar')}",
        f"• {_plural(snapshot.get('watch_total', 0), 'par cerca', 'pares cerca')} de zona de liquidez",
    ]

    top = [("setup", s) for s in setups] + [("sweep", pair) for pair in scored_sweeps]
    watch = [w for w in snapshot.get("watch", []) if (w["symbol"], w["direction"]) not in setup_keys]
    movers = snapshot.get("movers", [])

    def render(top_n: int, watch_n: int, movers_n: int) -> str:
        blocks = ["\n".join(header + summary)]
        if top:
            for idx, (kind, payload) in enumerate(top[:top_n]):
                medal = MEDALS[idx]
                if kind == "setup":
                    blocks.append(_render_setup(medal, payload, snapshot, regime, now_ts))
                else:
                    score, sweep = payload
                    blocks.append(_render_sweep(medal, sweep, score, regime, snapshot, now_ts))
            hidden = len(top) - top_n
            if hidden > 0:
                blocks.append(f"… y {_plural(hidden, 'oportunidad más', 'oportunidades más')} en seguimiento")
        else:
            blocks.append("😴 Sin setups ni barridos activos ahora. El radar sigue vigilando el mercado.")

        if watch_n and watch:
            lines = ["👁 En vigilancia (cerca de liquidez)"]
            for w in watch[:watch_n]:
                side = "mínimo" if w["direction"] == "LONG" else "máximo"
                tag = {"favor": " ✅", "contra": " ⚠️"}.get(w["alignment"], "")
                lines.append(
                    f"• {w['symbol']} {w['direction']} · a {w['distance_pct']:.1f}% del {side} 15M "
                    f"({_fmt_price(w['level'])}) · {_plural(w['zone_count'], 'toque', 'toques')} · vol {w['rel_volume']:.1f}x{tag}"
                )
            blocks.append("\n".join(lines))

        if movers_n and movers:
            lines = ["🔥 Movimientos inusuales"]
            for m in movers[:movers_n]:
                lines.append(f"• {m['symbol']} {m['change_1h_pct']:+.1f}% en 1h · volumen {m['vol_ratio']:.1f}x")
            blocks.append("\n".join(lines))

        blocks.append(
            "ℹ️ Score = calidad del setup confirmado. Score radar = calidad del barrido, frescura, "
            "alineación y volumen. Información orientativa, no es una recomendación de inversión."
        )
        return "\n\n".join(blocks)

    for config in ((3, 5, 3), (3, 3, 2), (3, 0, 0), (2, 0, 0), (1, 0, 0)):
        text = render(*config)
        if len(text) <= MAX_TELEGRAM_CHARS:
            return text
    return text
