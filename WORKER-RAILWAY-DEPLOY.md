# KERYNTIX MARKET DESK: despliegue del feed y los workers en Railway

La API y el proceso de workers son servicios distintos. El `Dockerfile` normal inicia solamente `uvicorn main:app`; no inicia `worker.market_feed`. Por eso la API puede contestar con un snapshot REST y, al mismo tiempo, no emitir ticks en vivo.

## Servicios necesarios

1. **API**: usa `Dockerfile` (el predeterminado) y publica el puerto `PORT` de Railway.
2. **Background workers**: crea un segundo servicio desde el mismo repositorio/commit y selecciona `Dockerfile.worker`. Ese contenedor inicia `python -m worker.supervisor`, que gestiona el feed de mercado, señales, ciclo de vida, planes, alertas, reconciliación y entrega de notificaciones.
3. **Telegram auth bot**: el backend lo documenta como proceso aparte. Crea otro servicio con el mismo repositorio y el comando de inicio `python -m worker.telegram_auth_bot` (puede usar `Dockerfile.worker` como imagen y sobrescribir el start command).
4. **Frontend**: `VITE_API_URL` debe apuntar a la URL HTTPS de la API. `VITE_WS_URL` debe estar vacío para derivar `wss://<mismo-host-api>/ws/events`, o apuntar expresamente a esa URL WebSocket. Como las variables `VITE_*` se incorporan al build, vuelve a desplegar el frontend después de cambiarlas.

## Variables que deben coincidir

API y ambos procesos worker deben usar exactamente el mismo `REDIS_URL` (la misma instancia/DB de Redis) y el mismo `FTRADE_DEPLOYMENT_NAME`. El namespace también debe coincidir con el que usa `/health`; si se omite `FTRADE_DEPLOYMENT_NAME`, `FTRADE_ENV` actúa como namespace de respaldo. En producción configura `FTRADE_ENV=production` y `BINANCE_FUTURES_WS=wss://fstream.binance.com/market` o deja el valor ya normalizado por `worker.market_feed`.

No publiques `REDIS_URL`, secretos de autenticación ni credenciales privadas en el frontend o en variables `VITE_*`.

## Comprobaciones tras el despliegue

- `GET /health`: revisar `workers.market-feed.status`, `market_feed.connected`, `market_feed.source`, `market_feed.last_price_update_at` y `market_feed.stale_seconds`.
- `GET /ready`: en producción debe devolver HTTP 200 solo cuando el estado de workers, configuración y almacenamiento está listo. Un 503 con `WORKER_NOT_HEALTHY:market-feed:not_started` indica que la API no ve un worker vivo en su namespace Redis.
- `GET /market/quotes`: revisar los headers `X-FTrade-Market-Status`, `X-FTrade-Market-Source`, `X-FTrade-Market-Connected` y `X-FTrade-Market-Stale-Seconds`. `source=rest` o `source=cache` no acredita ticks WebSocket.
- Logs del servicio worker: debe verse `Conectado a Binance Futures WebSocket` y no repetirse `WebSocket conectado, pero no llegan mini-tickers` o `El stream de precios se estancó`.
- En el navegador, DevTools → Network → WS: la conexión `/ws/events?ticket=...` debe quedar abierta y recibir frames `prices` / `tick`. No compartas el ticket de sesión en capturas ni logs públicos.

La configuración real de Railway no está incluida en los ZIP fuente, de modo que estas comprobaciones son necesarias para decidir si el bloqueo está en el despliegue, Redis, Binance o en el navegador.
