# Fase 2 — Web Push, canales y seguridad (2026-10-31)

- Añadido borrado idempotente de la suscripción Web Push exacta del usuario autenticado.
- Cookie refresh configurable mediante `AUTH_COOKIE_SAMESITE`; por defecto se conserva compatibilidad cross-site en producción (`none` + `Secure`).
- `AUTH_REFRESH_IN_BODY=false` hace que `/auth/refresh` ignore tokens enviados en JSON y dependa de la cookie HttpOnly.
- Cabeceras de seguridad HTTP añadidas a todas las respuestas FastAPI, con HSTS solo en producción.
- Las preferencias `in_app`, `telegram` y `web_push` ya existían en persistencia y workers; el frontend ahora las controla por separado.

## Despliegue y comprobación
- `VAPID_PUBLIC_KEY`, la clave privada equivalente y `VAPID_SUBJECT` deben coincidir en API y worker de entrega Web Push.
- El proceso `worker.web_push_delivery` debe estar activo para despachar las notificaciones; sin él, la suscripción se registra pero la entrega queda pendiente.
- Mantener el fallback de refresh en body/localStorage hasta verificar cookies HttpOnly en los navegadores objetivo. `AUTH_REFRESH_IN_BODY=false` y `VITE_AUTH_COOKIE_ONLY=true` deben desplegarse juntos.
- Producción fuerza la cookie `Secure`; `AUTH_COOKIE_SAMESITE=none` conserva compatibilidad con hosts realmente cross-site.
