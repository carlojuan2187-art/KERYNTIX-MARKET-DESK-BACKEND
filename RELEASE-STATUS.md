# Estado de consolidación FTrade — 25 de octubre de 2026

## Actualización — temporalidades de gráfico de 1 y 5 minutos (26 de octubre de 2026)

- La ruta `GET /market/candles` admite `timeframe=1m` y `timeframe=5m`, además de 15m, 1h, 4h y 1D.
- Se ajustó el mensaje de error de timeframe y el contrato de API.
- Se amplió `test/test_market_feed.py` para afirmar que los endpoints de 1m y 5m responden correctamente y solicitan esos intervalos a Binance REST.
- Suite ejecutada en esta copia: 307 pruebas aprobadas y 5 omitidas al excluir `test/test_ported_test_radar.py`, que tiene un fallo de importación preexistente de `tests.test_ported_test_regime`.

## Actualización — administración mediante teléfono verificado (26 de octubre de 2026)

- Se añadió `ADMIN_PHONE_NUMBERS` como alternativa sencilla a `ADMIN_USER_IDS`. Acepta uno o varios teléfonos internacionales, separados por comas.
- El backend compara esos números únicamente con el teléfono persistido y verificado de la cuenta; se aplica al perfil, token/sesión, permisos del panel administrativo, acceso a planes y workers.
- `ADMIN_USER_IDS` sigue funcionando para mantener compatibilidad con despliegues actuales.
- Prueba específica: el endpoint `/admin/summary` autoriza a la cuenta cuyo teléfono coincide con `ADMIN_PHONE_NUMBERS`, sin configurar `ADMIN_USER_IDS`.
- Suite actual en esta copia: **307 passed, 5 skipped** al excluir `test/test_ported_test_radar.py`. La ejecución sin excluirlo no completa la colección por un import preexistente de `tests.test_ported_test_regime` (la carpeta disponible en este repositorio se llama `test`). No se atribuye ese problema a este cambio.

## Validación ejecutada en esta sesión

### Backend

- Suite reproducida desde la copia consolidada: **311 passed, 5 skipped** (`PYTHONPATH=. pytest -q`). Los skips son 2 pruebas que requieren MongoDB/Redis reales y 3 pruebas de paridad que requieren `python-telegram-bot`.
- `python -m scripts.release_preflight --json`: **0 errores, 4 advertencias** en modo desarrollo. El informe exacto está en `docs/preflight-local.json`.
- `import main`: OK; la aplicación registra las rutas FastAPI.
- `compileall`: OK.
- Python disponible localmente: 3.13.5; la versión objetivo declarada es 3.11.
- Docker no está instalado; el stack de integración y la construcción de imagen no se ejecutaron.
- `pymongo` y `redis` no están instalados en el intérprete local. Sí están declarados en los requisitos del backend para instalarlos durante CI/imagen.

### Frontend

- Parser de TypeScript/TSX: **41 archivos analizados, 0 errores de sintaxis**.
- Escaneo de imports locales: **0 imports no resueltos**.
- El informe está en `ftrade-web/STATIC-SOURCE-CHECK.json`.
- `npm ci --offline` falló por un paquete ausente de la caché (`ENOTCACHED`). `npm ci` con red terminó por timeout al conectarse al registro npm.
- Por esa limitación, **no se ejecutaron `npx tsc --noEmit`, `npm run build` ni Playwright**. Se añadió `.github/workflows/frontend-ci.yml` para ejecutar estas verificaciones en GitHub Actions con Node 22.

## Cobertura funcional observada en el frontend

| Área | Estado en fuente actual |
|---|---|
| Inicio de sesión, verificación Telegram, sesión, perfil y sesiones | Adaptador API existente cuando `VITE_AUTH_MODE=api`; requiere validar con backend desplegado y cookies HTTPS. |
| Cotizaciones, velas y listado básico de señales | Algunas llamadas ya tienen adaptador API. Se debe probar el contrato real y el WebSocket conectado. |
| Watchlist, régimen, derivados, calendario, radar, flujo, movers, liquidaciones, overview y resultados | No se consideran integrados end-to-end. Las funciones demo fallan explícitamente en modo API donde se usa `requireDemoMode`. |
| Calculadora | El formulario aún no está conectado al endpoint real; en modo API se bloquea el cálculo de demostración. |
| Alertas y preferencias | Falta adaptar la interfaz a `/alerts/price` y `/account/alert-preferences`; la demo queda bloqueada en modo API. |
| Centro de notificaciones y Web Push | Backend disponible; el frontend y el Service Worker todavía deben conectarse. |
| Pagos Premium | El backend de verificación existe, pero el adaptador frontend no está completo. Los métodos de pago demo quedan bloqueados en modo API; no transferir fondos usando modo demo. |
| Referidos, soporte y administración | El backend tiene endpoints, pero faltan adaptadores frontend. Las acciones demo quedan bloqueadas en modo API. |

### Protección incorporada durante la consolidación

Se añadieron guardas `requireDemoMode(...)` para pagos, historial/verificación de pagos, referidos, administración, cálculo, alertas y preferencias demo. Cuando `VITE_AUTH_MODE=api`, esas funciones no deben simular pagos, activar Premium localmente ni modificar usuarios ficticios. **Esta protección no sustituye la conexión del front-end con los endpoints reales.**

## Bloqueadores antes de staging privado

1. Ejecutar la CI de backend con Python 3.11; corregir incompatibilidades que aparezcan.
2. Ejecutar las pruebas de integración contra MongoDB 7 y Redis 7 en GitHub Actions o Docker Compose.
3. Ejecutar el workflow frontend para validar `npm ci`, el typecheck y el build en Node 22.
4. Conectar los adaptadores React restantes a los endpoints existentes y alinear los modelos de request/response.
5. Configurar variables y secretos en un entorno privado; nunca incluir secretos en Git ni en variables `VITE_`.
6. Probar recorridos E2E de login, cuenta, plan, pago controlado, permisos, señales, soporte, admin y notificaciones.

## Conclusión

Este paquete **consolida las fuentes en dos repositorios separados** y deja CI/preflight preparados. Es una base de integración/staging, no una aprobación de producción. El build del frontend, la integración real de infraestructura y la integración end-to-end todavía no se han verificado en este entorno.
