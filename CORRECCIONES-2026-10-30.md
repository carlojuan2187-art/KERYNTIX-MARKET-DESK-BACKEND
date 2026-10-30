# Correcciones backend — 30 de octubre de 2026

## WebSocket

- Se agregó `_try_send_websocket_json` para comprobar los estados `client_state` y `application_state` antes de escribir.
- Las desconexiones y los errores de transporte (`WebSocketDisconnect`, `RuntimeError`, `OSError`) terminan el ciclo de envío sin reintentarlo en un socket muerto.
- La transformación del evento permanece aislada: un evento inválido se registra y no derriba la conexión; un transporte cerrado sí termina la sesión.
- El bloque `finally` cancela y espera todas las tareas de suscripción, evitando que queden pumps asociados a la conexión cerrada.

## Logging y Telegram

- API y workers usan `api.logging_config.configure_logging()`.
- Los resúmenes HTTPX de nivel INFO se conservan para observabilidad de mercado. El filtro redacta el token de `https://api.telegram.org/bot...` como `[REDACTED]`.
- `httpcore` DEBUG se mantiene apagado para evitar detalles de bajo nivel no necesarios.
- Los errores de transporte de Telegram ya no encadenan excepciones HTTPX con la URL secreta.

## Pruebas

Añadidas pruebas específicas en `test/test_websocket_send_safety.py` y `test/test_logging_config.py`. Ejecuta `pytest -q` desde la raíz del backend.

## Operación requerida

**Rotar el token Telegram expuesto en el log anterior.** Desde BotFather revoca el token anterior y genera uno nuevo; configura el nuevo valor en `TELEGRAM_AUTH_BOT_TOKEN` en todos los servicios Railway que carguen este backend (API, autenticación Telegram y entrega de notificaciones). Reinicia los servicios. No pegues el token en chats ni en logs. Esta implementación no puede revocar credenciales ni borrar los logs históricos de Railway.
