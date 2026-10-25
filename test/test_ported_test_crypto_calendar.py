import importlib.util
import unittest
from datetime import date, datetime, timedelta, timezone

from engine import crypto_calendar as cc
from engine import derivatives as dv

UTC = timezone.utc
HAS_TELEGRAM = importlib.util.find_spec("telegram") is not None
if HAS_TELEGRAM:
    from app.menus import calendar_menu, main_menu

SAMPLE_ICS = "\r\n".join([
    "BEGIN:VCALENDAR",
    "BEGIN:VEVENT",
    "DTSTART;TZID=US-Eastern:20261110T083000",
    "SUMMARY:Consumer Price Index for October 2026",
    "END:VEVENT",
    "BEGIN:VEVENT",
    "DTSTART:20261204T133000Z",
    "SUMMARY:Employment Situation for",
    "  November 2026",                       # línea plegada
    "END:VEVENT",
    "BEGIN:VEVENT",
    "DTSTART;VALUE=DATE:20261112",            # sólo fecha -> 08:30 ET
    "SUMMARY:Consumer Price Index for October 2026 (revised)",
    "END:VEVENT",
    "BEGIN:VEVENT",
    "DTSTART;TZID=US-Eastern:20261110T083000",
    "SUMMARY:Real Earnings for October 2026",
    "END:VEVENT",
    "BEGIN:VEVENT",
    "DTSTART;TZID=US-Eastern:20261028T100000",
    "SUMMARY:Employment Situation of Veterans for Annual 2025",
    "END:VEVENT",
    "BEGIN:VEVENT",
    "DTSTART:basura",
    "SUMMARY:Consumer Price Index for September 2026",
    "END:VEVENT",
    "END:VCALENDAR",
])


class FomcTests(unittest.TestCase):
    def test_dates_sorted_and_eight_per_year(self):
        days = sorted(cc.FOMC_DECISION_DATES)
        self.assertEqual(days, list(cc.FOMC_DECISION_DATES))
        for year in (2026, 2027):
            self.assertEqual(sum(1 for d in days if d.year == year), 8)
        for d in days:
            self.assertIn(d.weekday(), (2, 1))  # decisión en miércoles (o martes tras ajustes)

    def test_decision_time_follows_us_dst(self):
        start = datetime(2026, 10, 1, tzinfo=UTC)
        events = {e["when"]: e for e in cc.fomc_events(start, datetime(2026, 12, 31, tzinfo=UTC))}
        self.assertIn(datetime(2026, 10, 28, 18, 0, tzinfo=UTC), events)   # EDT: 14:00 ET = 18:00 UTC
        self.assertIn(datetime(2026, 12, 9, 19, 0, tzinfo=UTC), events)    # EST: 14:00 ET = 19:00 UTC
        dec = events[datetime(2026, 12, 9, 19, 0, tzinfo=UTC)]
        self.assertIn("dot plot", dec["note"])
        self.assertEqual(events[datetime(2026, 10, 28, 18, 0, tzinfo=UTC)]["note"], "")


class DeribitTests(unittest.TestCase):
    def test_fridays_only_at_0800_utc_with_kinds(self):
        start = datetime(2026, 10, 23, 0, 0, tzinfo=UTC)         # viernes
        events = cc.deribit_expiries(start, datetime(2026, 12, 31, tzinfo=UTC))
        self.assertTrue(events)
        for e in events:
            self.assertEqual((e["when"].weekday(), e["when"].hour, e["when"].minute), (4, 8, 0))
        by_day = {e["when"].date(): e for e in events}
        self.assertEqual(by_day[date(2026, 10, 23)]["title"], "Vencimiento semanal Deribit")
        self.assertEqual(by_day[date(2026, 10, 30)]["title"], "Vencimiento mensual Deribit")   # último viernes de oct
        self.assertEqual(by_day[date(2026, 12, 25)]["title"], "Vencimiento trimestral Deribit")
        self.assertEqual(by_day[date(2026, 12, 25)]["impact"], "alto")
        self.assertEqual(by_day[date(2026, 10, 30)]["impact"], "medio")

    def test_quarterly_in_september_and_march(self):
        events = cc.deribit_expiries(datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 30, tzinfo=UTC))
        self.assertEqual([e["title"] for e in events if e["when"].day == 25], ["Vencimiento trimestral Deribit"])
        events = cc.deribit_expiries(datetime(2027, 3, 1, tzinfo=UTC), datetime(2027, 3, 31, tzinfo=UTC))
        self.assertEqual([e["when"].day for e in events if "trimestral" in e["title"]], [26])

    def test_window_bounds_respected(self):
        events = cc.deribit_expiries(datetime(2026, 10, 23, 9, 0, tzinfo=UTC), datetime(2026, 10, 29, tzinfo=UTC))
        self.assertEqual(events, [])  # el viernes de hoy ya pasó a las 08:00; el próximo es el 30


class FundingTests(unittest.TestCase):
    def test_next_funding_standard_schedule(self):
        f = lambda h, m=0: cc.next_funding_utc(datetime(2026, 10, 23, h, m, tzinfo=UTC))
        self.assertEqual(f(0, 0).hour, 8)      # exactamente a la hora: el siguiente
        self.assertEqual(f(7, 59).hour, 8)
        self.assertEqual(f(8, 1).hour, 16)
        nxt = f(16, 30)
        self.assertEqual((nxt.day, nxt.hour), (24, 0))

    def test_binance_next_funding_ms_is_cached(self):
        dv.clear_cache()
        calls = []

        def get_json(path, params):
            calls.append(path)
            return {"nextFundingTime": "1800000000000"}
        self.assertEqual(dv.get_next_funding_ms("BTCUSDT", now=100.0, get_json=get_json), 1.8e12)
        dv.get_next_funding_ms("BTCUSDT", now=101.0, get_json=get_json)
        self.assertEqual(len(calls), 1)

    def test_binance_failure_returns_none(self):
        dv.clear_cache()

        def boom(path, params):
            raise RuntimeError("x")
        self.assertIsNone(dv.get_next_funding_ms("ETHUSDT", now=1.0, get_json=boom))


class BlsParseTests(unittest.TestCase):
    def setUp(self):
        self.events = cc.parse_bls_ics(SAMPLE_ICS)

    def test_only_cpi_and_nfp_are_kept(self):
        self.assertEqual([e["kind"] for e in self.events], ["cpi", "cpi", "nfp"])
        titles = " ".join(e["title"] for e in self.events)
        self.assertNotIn("Veterans", titles)

    def test_time_conversions(self):
        whens = [e["when"] for e in self.events]
        self.assertEqual(whens[0], datetime(2026, 11, 10, 13, 30, tzinfo=UTC))   # 08:30 EST
        self.assertEqual(whens[1], datetime(2026, 11, 12, 13, 30, tzinfo=UTC))   # sólo fecha -> 08:30 ET
        self.assertEqual(whens[2], datetime(2026, 12, 4, 13, 30, tzinfo=UTC))    # ya en UTC
        self.assertTrue(all(e["impact"] == "alto" for e in self.events))

    def test_garbage_input(self):
        self.assertEqual(cc.parse_bls_ics(""), [])
        self.assertEqual(cc.parse_bls_ics("<html>403 Forbidden</html>"), [])


class BlsCacheTests(unittest.TestCase):
    def setUp(self):
        cc.clear_cache()

    def tearDown(self):
        cc.clear_cache()

    def test_cached_for_ttl(self):
        calls = []

        def text():
            calls.append(1)
            return SAMPLE_ICS
        first = cc.get_bls_events(now=1000.0, get_text=text)
        self.assertTrue(first["ok"])
        cc.get_bls_events(now=1000.0 + cc.BLS_CACHE_SECONDS - 1, get_text=text)
        self.assertEqual(len(calls), 1)
        cc.get_bls_events(now=1000.0 + cc.BLS_CACHE_SECONDS + 1, get_text=text)
        self.assertEqual(len(calls), 2)

    def test_failure_is_negative_cached_briefly(self):
        calls = []

        def boom():
            calls.append(1)
            raise RuntimeError("403")
        result = cc.get_bls_events(now=1.0, get_text=boom)
        self.assertEqual(result, {"events": [], "ok": False})
        cc.get_bls_events(now=2.0, get_text=boom)
        self.assertEqual(len(calls), 1)                                   # no martilla al BLS
        cc.get_bls_events(now=1.0 + cc.BLS_RETRY_SECONDS + 1, get_text=boom)
        self.assertEqual(len(calls), 2)

    def test_stale_data_kept_when_refresh_fails(self):
        cc.get_bls_events(now=0.0, get_text=lambda: SAMPLE_ICS)

        def boom():
            raise RuntimeError("x")
        later = cc.get_bls_events(now=cc.BLS_CACHE_SECONDS + 5, get_text=boom)
        self.assertTrue(later["ok"])
        self.assertEqual(len(later["events"]), 3)

    def test_unexpected_format_is_not_ok(self):
        self.assertFalse(cc.get_bls_events(now=1.0, get_text=lambda: "<html></html>")["ok"])


class BuildAndFormatTests(unittest.TestCase):
    NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)     # lunes; FOMC el miércoles 28

    def bls(self):
        return {"events": cc.parse_bls_ics(SAMPLE_ICS), "ok": True}

    def test_sorted_and_windowed(self):
        cal = cc.build_calendar(self.NOW, days=7, bls=self.bls())
        whens = [e["when"] for e in cal["events"]]
        self.assertEqual(whens, sorted(whens))
        self.assertTrue(all(self.NOW - timedelta(minutes=30) <= w <= self.NOW + timedelta(days=7) for w in whens))
        kinds = {e["kind"] for e in cal["events"]}
        self.assertEqual(kinds, {"fomc", "expiry"})       # el CPI de nov queda fuera de 7 días

    def test_30_days_with_min_impact_hides_weekly(self):
        cal = cc.build_calendar(self.NOW, days=30, min_impact="medio", bls=self.bls())
        self.assertFalse(any(e["title"].endswith("semanal Deribit") for e in cal["events"]))
        self.assertIn("cpi", {e["kind"] for e in cal["events"]})

    def test_message_contents(self):
        cal = cc.build_calendar(self.NOW, days=7, bls=self.bls())
        text = cc.format_calendar_message(
            cal, self.NOW, session={"name": "Londres + Nueva York", "liquidity": "máxima liquidez"},
            tz_name="America/Havana")
        for expected in ("CALENDARIO CRIPTO", "Sesión ahora: Londres + Nueva York", "Próximo funding: en 4h 0m",
                         "FOMC: decisión de tasas", "mié 28 oct · 18:00 UTC", "Havana", "en 2d 6h",
                         "Vencimiento mensual Deribit", "no modifica ni filtra señales"):
            self.assertIn(expected, text)
        self.assertNotIn("CPI y NFP no disponibles", text)

    def test_high_impact_within_24h_warns(self):
        now = datetime(2026, 10, 28, 6, 0, tzinfo=UTC)
        text = cc.format_calendar_message(cc.build_calendar(now, bls=self.bls()), now)
        self.assertIn("alto impacto en menos de 24 h", text)
        later = datetime(2026, 10, 26, 6, 0, tzinfo=UTC)
        self.assertNotIn("alto impacto en menos de 24 h",
                         cc.format_calendar_message(cc.build_calendar(later, bls=self.bls()), later))

    def test_bls_unavailable_is_disclosed_and_rest_still_works(self):
        cal = cc.build_calendar(self.NOW, bls={"events": [], "ok": False})
        text = cc.format_calendar_message(cal, self.NOW)
        self.assertIn("CPI y NFP no disponibles", text)
        self.assertIn("FOMC", text)

    def test_event_just_started_shows_ahora(self):
        now = datetime(2026, 10, 28, 18, 5, tzinfo=UTC)
        text = cc.format_calendar_message(cc.build_calendar(now, bls=self.bls()), now)
        self.assertIn("ahora", text)

    def test_invalid_timezone_falls_back(self):
        cal = cc.build_calendar(self.NOW, bls=self.bls())
        self.assertIn("UTC", cc.format_calendar_message(cal, self.NOW, tz_name="Nope/Nowhere"))

    def test_countdown(self):
        now = datetime(2026, 1, 1, tzinfo=UTC)
        self.assertEqual(cc.fmt_countdown(now + timedelta(minutes=30), now), "en 30m")
        self.assertEqual(cc.fmt_countdown(now + timedelta(hours=5, minutes=3), now), "en 5h 3m")
        self.assertEqual(cc.fmt_countdown(now + timedelta(days=3, hours=2), now), "en 3d 2h")
        self.assertEqual(cc.fmt_countdown(now - timedelta(minutes=5), now), "ahora")

    def test_message_fits_a_phone_screenful_or_two(self):
        cal = cc.build_calendar(self.NOW, days=7, bls=self.bls())
        self.assertLess(len(cc.format_calendar_message(cal, self.NOW).splitlines()), 30)


@unittest.skipUnless(HAS_TELEGRAM, "python-telegram-bot no instalado")
class MenuWiringTests(unittest.TestCase):
    def cbs(self, markup):
        return [b.callback_data for row in markup.inline_keyboard for b in row]

    def test_buttons(self):
        self.assertIn("calendar", self.cbs(main_menu()))
        self.assertEqual(self.cbs(calendar_menu(7)), ["calendar", "calendar_30", "back_menu"])
        self.assertEqual(self.cbs(calendar_menu(30)), ["calendar_30", "calendar", "back_menu"])


if __name__ == "__main__":
    unittest.main()
