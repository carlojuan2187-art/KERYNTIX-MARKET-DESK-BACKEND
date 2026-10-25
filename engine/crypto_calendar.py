# app/crypto_calendar.py
"""📅 Calendario cripto: eventos que mueven el mercado, sólo con fuentes públicas (sin claves).

Fuentes:
- FOMC: fechas oficiales de la Reserva Federal (decisión 2:00 PM ET del segundo día).
  Se mantienen en `FOMC_DECISION_DATES`; revisar cuando la Fed publique el año siguiente.
- CPI y NFP (Employment Situation): calendario iCalendar público del BLS
  (https://www.bls.gov/schedule/news_release/bls.ics), con caché. Si no se puede
  descargar, el calendario sigue funcionando sin esos dos eventos y lo indica.
- Vencimientos de opciones/futuros de Deribit: se calculan por regla (viernes 08:00 UTC;
  mensual = último viernes del mes; trimestral = último viernes de mar/jun/sep/dic).

La lógica es pura salvo `fetch_bls_ics` (red) y la caché. Es informativo: no filtra señales.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

UTC = timezone.utc
ET = ZoneInfo("America/New_York")

BLS_ICS_URL = os.getenv("BLS_ICS_URL", "https://www.bls.gov/schedule/news_release/bls.ics")
BLS_USER_AGENT = os.getenv("BLS_USER_AGENT", "Mozilla/5.0 (compatible; FTradeBot/1.0)")
BLS_TIMEOUT = int(os.getenv("BLS_TIMEOUT", "10"))
BLS_CACHE_SECONDS = max(600, int(os.getenv("BLS_CACHE_SECONDS", "21600")))   # 6 h
BLS_RETRY_SECONDS = 600                                                      # reintento tras fallo
USER_TIMEZONE = os.getenv("USER_TIMEZONE", "America/Havana")

# Hora habitual de publicación del BLS (CPI y Employment Situation) si el .ics sólo trae fecha.
BLS_DEFAULT_ET = (8, 30)

IMPACT_ICON = {"alto": "🔴", "medio": "🟡", "bajo": "⚪"}
_IMPACT_RANK = {"bajo": 0, "medio": 1, "alto": 2}

# Segundo día de cada reunión FOMC (decisión 2:00 PM ET). Fuente: federalreserve.gov
# (2026 confirmado; 2027 y enero 2028 son el calendario tentativo publicado por la Fed).
# True = reunión con Resumen de Proyecciones Económicas (dot plot).
FOMC_DECISION_DATES: Dict[date, bool] = {
    date(2026, 1, 28): False, date(2026, 3, 18): True, date(2026, 4, 29): False,
    date(2026, 6, 17): True, date(2026, 7, 29): False, date(2026, 9, 16): True,
    date(2026, 10, 28): False, date(2026, 12, 9): True,
    date(2027, 1, 27): False, date(2027, 3, 17): True, date(2027, 4, 28): False,
    date(2027, 6, 9): True, date(2027, 7, 28): False, date(2027, 9, 15): True,
    date(2027, 10, 27): False, date(2027, 12, 8): True,
    date(2028, 1, 26): False,
}

_WEEKDAYS = ("lun", "mar", "mié", "jue", "vie", "sáb", "dom")
_MONTHS = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")
_LINE = "━━━━━━━━━━━━━━━━━━━━"


# ------------------------------------------------------------------
# Eventos calculados por regla / lista fija
# ------------------------------------------------------------------
def _event(kind: str, title: str, when: datetime, impact: str, note: str = "") -> Dict[str, Any]:
    return {"kind": kind, "title": title, "when": when.astimezone(UTC), "impact": impact, "note": note}


def fomc_events(start: datetime, end: datetime) -> List[Dict[str, Any]]:
    events = []
    for day, has_sep in sorted(FOMC_DECISION_DATES.items()):
        when = datetime(day.year, day.month, day.day, 14, 0, tzinfo=ET)
        if start <= when <= end:
            note = "con proyecciones (dot plot)" if has_sep else ""
            events.append(_event("fomc", "FOMC: decisión de tasas", when, "alto", note))
    return events


def _is_last_friday(day: date) -> bool:
    return day.weekday() == 4 and (day + timedelta(days=7)).month != day.month


def deribit_expiries(start: datetime, end: datetime) -> List[Dict[str, Any]]:
    """Vencimientos de opciones/futuros de Deribit: cada viernes 08:00 UTC."""
    events = []
    day = start.astimezone(UTC).date()
    last_day = end.astimezone(UTC).date()
    while day <= last_day:
        if day.weekday() == 4:
            when = datetime(day.year, day.month, day.day, 8, 0, tzinfo=UTC)
            if start <= when <= end:
                if _is_last_friday(day) and day.month in (3, 6, 9, 12):
                    events.append(_event("expiry", "Vencimiento trimestral Deribit", when, "alto",
                                         "opciones y futuros; suele mover el precio"))
                elif _is_last_friday(day):
                    events.append(_event("expiry", "Vencimiento mensual Deribit", when, "medio",
                                         "opciones y futuros"))
                else:
                    events.append(_event("expiry", "Vencimiento semanal Deribit", when, "bajo"))
        day += timedelta(days=1)
    return events


def next_funding_utc(now: datetime) -> datetime:
    """Próximo funding estándar de 8 h (00:00, 08:00, 16:00 UTC). Respaldo si Binance no responde."""
    now = now.astimezone(UTC)
    for hour in (0, 8, 16):
        candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if candidate > now:
            return candidate
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


# ------------------------------------------------------------------
# BLS: parseo del iCalendar
# ------------------------------------------------------------------
_CPI_RE = re.compile(r"^Consumer Price Index for\b", re.IGNORECASE)
_NFP_RE = re.compile(r"^Employment Situation for\b", re.IGNORECASE)  # excluye "…of Veterans"


def _unescape(value: str) -> str:
    return value.replace("\\,", ",").replace("\\;", ";").replace("\\n", " ").replace("\\N", " ").strip()


def _parse_dtstart(params: str, value: str) -> Optional[datetime]:
    value = value.strip()
    try:
        if value.endswith("Z"):
            return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        if "T" in value:
            naive = datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
            tzid = re.search(r"TZID=([^;:]+)", params or "")
            zone = ET
            if tzid:
                try:
                    zone = ZoneInfo(tzid.group(1).strip().strip('"'))
                except Exception:
                    zone = ET  # "US-Eastern" y similares: el BLS publica en hora del Este
            return naive.replace(tzinfo=zone).astimezone(UTC)
        day = datetime.strptime(value[:8], "%Y%m%d")
        hour, minute = BLS_DEFAULT_ET
        return day.replace(hour=hour, minute=minute, tzinfo=ET).astimezone(UTC)
    except ValueError:
        return None


def parse_bls_ics(text: str) -> List[Dict[str, Any]]:
    """Extrae CPI y NFP de un iCalendar del BLS. Tolera plegado de líneas y formatos de fecha."""
    unfolded = re.sub(r"\r?\n[ \t]", "", text or "")
    events: List[Dict[str, Any]] = []
    summary = None
    dtstart = None
    inside = False
    for raw in unfolded.splitlines():
        line = raw.strip()
        if line == "BEGIN:VEVENT":
            inside, summary, dtstart = True, None, None
            continue
        if line == "END:VEVENT":
            if inside and summary and dtstart:
                if _CPI_RE.match(summary):
                    events.append(_event("cpi", "CPI EE. UU. (inflación)", dtstart, "alto"))
                elif _NFP_RE.match(summary):
                    events.append(_event("nfp", "NFP EE. UU. (empleo)", dtstart, "alto"))
            inside = False
            continue
        if not inside or ":" not in line:
            continue
        head, value = line.split(":", 1)
        name, _, params = head.partition(";")
        name = name.upper()
        if name == "SUMMARY":
            summary = _unescape(value)
        elif name == "DTSTART":
            dtstart = _parse_dtstart(params, value)
    return sorted(events, key=lambda e: e["when"])


def fetch_bls_ics() -> str:
    import requests  # import tardío: el resto del módulo no necesita red

    response = requests.get(BLS_ICS_URL, headers={"User-Agent": BLS_USER_AGENT}, timeout=BLS_TIMEOUT)
    response.raise_for_status()
    return response.text


_bls_cache: Dict[str, Any] = {"at": 0.0, "ttl": 0.0, "events": [], "ok": False}
_bls_lock = threading.Lock()


def get_bls_events(now: Optional[float] = None,
                   get_text: Optional[Callable[[], str]] = None) -> Dict[str, Any]:
    """CPI/NFP del BLS con caché de 6 h; tras un fallo reintenta a los 10 min."""
    current = time.time() if now is None else now
    with _bls_lock:
        if _bls_cache["at"] and current - _bls_cache["at"] < _bls_cache["ttl"]:
            return {"events": list(_bls_cache["events"]), "ok": _bls_cache["ok"]}
    try:
        events = parse_bls_ics((get_text or fetch_bls_ics)())
        ttl = BLS_CACHE_SECONDS if events else BLS_RETRY_SECONDS  # vacío = formato inesperado
    except Exception as exc:
        logger.warning("Calendario BLS no disponible: %s", exc)
        events, ttl = [], BLS_RETRY_SECONDS
    with _bls_lock:
        if events:
            _bls_cache.update(at=current, ttl=ttl, events=events, ok=True)
        else:  # fallo o respuesta vacía: se conservan los últimos datos buenos (si los hay)
            _bls_cache.update(at=current, ttl=ttl, ok=bool(_bls_cache["events"]))
        return {"events": list(_bls_cache["events"]), "ok": _bls_cache["ok"]}


def clear_cache() -> None:
    with _bls_lock:
        _bls_cache.update(at=0.0, ttl=0.0, events=[], ok=False)


# ------------------------------------------------------------------
# Composición
# ------------------------------------------------------------------
def build_calendar(now: datetime, days: int = 7, min_impact: str = "bajo",
                   bls: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Eventos de los próximos `days` días (ordenados) y si el BLS estaba disponible."""
    now = now.astimezone(UTC)
    start = now - timedelta(minutes=30)  # el evento de hace minutos aún se muestra como "ahora"
    end = now + timedelta(days=days)
    bls = bls if bls is not None else get_bls_events()
    events = fomc_events(start, end) + deribit_expiries(start, end)
    events += [e for e in bls["events"] if start <= e["when"] <= end]
    floor = _IMPACT_RANK.get(min_impact, 0)
    events = [e for e in events if _IMPACT_RANK[e["impact"]] >= floor]
    events.sort(key=lambda e: e["when"])
    return {"events": events, "bls_ok": bool(bls["ok"]), "days": days}


# ------------------------------------------------------------------
# Formato
# ------------------------------------------------------------------
def _local_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("UTC")


def fmt_countdown(when: datetime, now: datetime) -> str:
    seconds = (when - now).total_seconds()
    if seconds < 60:
        return "ahora"
    minutes = int(seconds // 60)
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    if days:
        return f"en {days}d {hours}h"
    if hours:
        return f"en {hours}h {mins}m"
    return f"en {mins}m"


def format_event(event: Dict[str, Any], now: datetime, tz_name: str = USER_TIMEZONE) -> str:
    when = event["when"]
    zone = _local_zone(tz_name)
    local = when.astimezone(zone)
    city = tz_name.split("/")[-1].replace("_", " ")
    head = f"{IMPACT_ICON[event['impact']]} {event['title']}"
    if event.get("note"):
        head += f" ({event['note']})"
    day = f"{_WEEKDAYS[local.weekday()]} {local.day} {_MONTHS[local.month - 1]}"
    return (
        f"{head}\n"
        f"   {day} · {when.strftime('%H:%M')} UTC · {local.strftime('%H:%M')} {city} · "
        f"{fmt_countdown(when, now)}"
    )


def format_calendar_message(
    calendar: Dict[str, Any],
    now: datetime,
    session: Optional[Dict[str, str]] = None,
    next_funding: Optional[datetime] = None,
    tz_name: str = USER_TIMEZONE,
) -> str:
    now = now.astimezone(UTC)
    days = calendar["days"]
    lines: List[str] = ["📅 CALENDARIO CRIPTO", _LINE]
    if session:
        lines.append(f"Sesión ahora: {session['name']} · {session['liquidity']}")
    funding = next_funding or next_funding_utc(now)
    lines.append(f"Próximo funding: {fmt_countdown(funding, now)} ({funding.strftime('%H:%M')} UTC)")

    events = calendar["events"]
    lines += ["", f"Próximos {days} días" if days != 1 else "Próximo día"]
    if events:
        for event in events:
            lines.append(format_event(event, now, tz_name))
    else:
        lines.append("Sin eventos relevantes en este periodo.")

    soon = [e for e in events if e["impact"] == "alto" and 0 <= (e["when"] - now).total_seconds() <= 24 * 3600]
    if soon:
        lines += [
            "",
            "⚠️ Hay un evento de alto impacto en menos de 24 h: la volatilidad suele subir "
            "y los barridos pueden ser más bruscos. Considera reducir tamaño o esperar.",
        ]
    if not calendar["bls_ok"]:
        lines += ["", "ℹ️ CPI y NFP no disponibles ahora (no se pudo leer el calendario del BLS)."]

    lines += [
        _LINE,
        "🔴 alto · 🟡 medio · ⚪ bajo. Fuentes públicas: Reserva Federal, BLS y reglas de Deribit. "
        "Informativo: no modifica ni filtra señales.",
    ]
    return "\n".join(lines)
