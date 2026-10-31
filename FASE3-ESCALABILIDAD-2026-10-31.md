# FTrade — Fase 3: escalabilidad y rendimiento

Fecha: 31/10/2026

## Alcance

Esta fase optimiza la capacidad de procesamiento y el uso de recursos sin cambiar la estrategia de señales, sus indicadores, sus umbrales, sus filtros ni las reglas de acceso por plan. La prueba gratuita de cinco días conserva acceso completo equivalente a Premium.

## Cambios implementados

1. **Escáner de señales con grupo fijo de workers.** Se sustituyó la creación de una tarea asyncio por símbolo por una cola y un número fijo de workers limitado por `SCAN_CONCURRENCY`. Se conserva el ritmo de solicitudes configurado para Binance.
2. **Distribución de avisos paginada.** Los usuarios y las alertas de precio se consultan mediante cursores ordenados e índices, eliminando el límite duro anterior de 5.000 usuarios/alertas. El despacho procesa cada página con concurrencia acotada.
3. **Seguimiento de operaciones por símbolo.** El monitor agrupa señales abiertas por par y reutiliza las velas de un mismo símbolo. Los historiales largos se dividen en ventanas consecutivas para no dejar sin velas recientes a señales nuevas si una señal antigua lleva mucho tiempo abierta. Se mantienen `advance_trade`, los límites temporales por señal y el compare-and-set de persistencia.
4. **Pools de conexiones acotados.** Redis, Binance REST y los seis repositorios MongoDB aplican límites de conexiones configurables.
5. **Backoff respetando Binance.** Las respuestas 418/429 respetan `Retry-After` (incluyendo fecha HTTP) antes de reintentar, para no agravar rate limits.

## Variables nuevas/relevantes

Valores recomendados incluidos en `.env.example`:

```env
BINANCE_REST_MAX_CONNECTIONS=8
BINANCE_REST_CONCURRENCY=4
REDIS_MAX_CONNECTIONS=32
MONGODB_MAX_POOL_SIZE=20
MONGODB_MIN_POOL_SIZE=0
MONGODB_MAX_CONNECTING=4
MONGODB_WAIT_QUEUE_TIMEOUT_MS=5000
MONGODB_MAX_IDLE_TIME_MS=60000
ALERT_DISPATCH_PAGE_SIZE=500
ALERT_DISPATCH_CONCURRENCY=12
LIFECYCLE_REST_CONCURRENCY=4
```

Los límites se validan y se acotan desde el código. Si Railway ya tiene variables con esos nombres, puede conservarlas; si no, los valores por defecto son los anteriores. API y workers deben compartir el mismo `REDIS_URL`, `MONGODB_URI` y `DATABASE_NAME`, como en las fases previas.

## Archivos backend modificados

- `.env.example`
- `api/cache.py`
- `api/repositories/auth.py`
- `api/repositories/business.py`
- `api/repositories/lifecycle.py`
- `api/repositories/signals.py`
- `api/repositories/sweeps.py`
- `api/repositories/user_features.py`
- `worker/binance_rest.py`
- `worker/signal_lifecycle.py`
- `worker/signal_scanner.py`
- `worker/user_alerts.py`

## Archivos nuevos

- `api/mongo_config.py`
- `test/test_mongo_config.py`
- `FASE3-ESCALABILIDAD-2026-10-31.md`

## Pruebas añadidas

- Paginación de avisos de señales más allá del límite histórico de 5.000 usuarios (5.007 cuentas en prueba).
- Paginación de avisos de mercado y alertas de precio.
- Grupo fijo de workers del escáner con lista amplia de símbolos.
- Reutilización de velas para varias señales del mismo símbolo.
- Ventanas históricas consecutivas para evitar que una señal antigua deje sin datos a las recientes.
- Límites y valores por defecto del pool MongoDB.
- Respeto de `Retry-After` en respuestas 429 de Binance.

## Validación local

- `python -m compileall -q api worker domain engine`: correcto.
- Suite backend completa: 386 aprobadas, 5 omitidas (2 pruebas requieren MongoDB/Redis reales y 3 requieren el paquete opcional `python-telegram-bot`).
- Build Vite/Playwright: no se ejecutó en esta fase porque el frontend no cambió y el entorno no tiene `node_modules`.
- No se ejecutaron pruebas de carga contra Railway ni servicios externos reales.

## Límites y siguiente paso

Esta entrega prepara límites y elimina trabajo duplicado, pero no certifica capacidad de producción. Antes de escalar tráfico real se recomienda ejecutar pruebas de carga controladas en un entorno de staging con MongoDB y Redis reales, observar CPU/memoria, conexiones Mongo/Redis, latencia Binance, duración de cada ciclo de workers y profundidad de colas. También debe generarse un lock reproducible de dependencias Python en un entorno de build controlado; no se ha cambiado el conjunto de versiones de producción en esta fase.
