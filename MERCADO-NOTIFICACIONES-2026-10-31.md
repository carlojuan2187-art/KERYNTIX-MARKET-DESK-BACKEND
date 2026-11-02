# KERYNTIX MARKET DESK — cambios de derivados y notificaciones (2026-10-31)

## Derivados / Binance
- Se quitaron las consultas a `/futures/data/topLongShortAccountRatio` y `/futures/data/topLongShortPositionRatio` del servicio web de analítica.
- Esos endpoints dejaron de estar permitidos en el cliente REST de analítica; no se requiere ni se utiliza `BINANCE_API_KEY` para la vista web de derivados.
- Se eliminó `BINANCE_API_KEY` del `.env.example` porque ya no es necesaria para este flujo.
- El response conserva temporalmente los campos Top Traders vacíos para evitar romper clientes antiguos; ya no se emiten metadatos de fuente de esos datos.
- Funding y base calculan cobertura únicamente sobre puntos con valores numéricos válidos. La base por período se conserva aunque no exista el campo anualizado. No se fabrica una anualización.

## Notificaciones de señales
- El scanner llama a `UserAlertProcessor.notify_new_signal` únicamente después de persistir una señal nueva.
- Las notificaciones usan el tipo/preferencia `signals`, respetan acceso de usuario y visibilidad Free/Premium, y pasan por el mismo almacenamiento/outbox que el resto de alertas.
- El sistema existente entrega in-app y encola Telegram/Web Push según las preferencias y destinos configurados.
- La clave de evento `signal-created:<signal_id>` evita duplicados ante reintentos.
- Un fallo en el despacho de avisos no revierte ni bloquea el guardado de la señal.

## Validación backend
- `python -m compileall -q api worker test`: correcto.
- `pytest -q`: 349 aprobadas, 5 omitidas.
- Las omitidas requieren MongoDB/Redis reales o la dependencia opcional `python-telegram-bot`.
