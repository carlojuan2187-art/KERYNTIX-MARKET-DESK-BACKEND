from api.services.localization import localize_notification


def test_new_signal_notification_is_localized_for_english_users():
    title, message = localize_notification(
        "Nueva señal LONG · BTCUSDT",
        "FTrade publicó una nueva señal LONG para BTCUSDT.\nEntrada: 104,000 · Stop-loss: 102,000 · Objetivos: 106,000 / 108,000 · Score: 87.0/100",
        "en",
    )
    assert title == "New LONG signal · BTCUSDT"
    assert "FTrade published a new LONG signal for BTCUSDT." in message
    assert "Entry: 104,000" in message
    assert "Targets: 106,000 / 108,000" in message


def test_new_signal_notification_remains_spanish_for_spanish_users():
    title = "Nueva señal SHORT · ETHUSDT"
    message = "FTrade publicó una nueva señal SHORT para ETHUSDT.\nEntrada: 3,000"
    assert localize_notification(title, message, "es") == (title, message)


def test_market_anomaly_notification_is_localized():
    title, message = localize_notification(
        "Aviso PUMP · SOLUSDT",
        "SOLUSDT se movió +4.20% en 5 min con volumen x3.1 de lo normal. Este aviso describe una anomalía de mercado, no una señal de entrada.",
        "en",
    )
    assert title == "PUMP alert · SOLUSDT"
    assert "moved +4.20% in 5 min" in message
    assert "not an entry signal" in message


def test_price_alert_notification_is_localized():
    title, message = localize_notification(
        "Alerta de precio · BTCUSDT",
        "BTCUSDT: el precio actual (70,000) alcanzó o superó tu nivel de alerta (69,500).",
        "en",
    )
    assert title == "Price alert · BTCUSDT"
    assert "reached or exceeded your alert level" in message


def test_business_notification_is_localized():
    title, message = localize_notification(
        "Un referido activó Premium",
        "Una persona invitada mediante tu enlace ha activado Premium.",
        "en",
    )
    assert title == "A referral activated Premium"
    assert message == "Someone invited through your referral link has activated Premium."
