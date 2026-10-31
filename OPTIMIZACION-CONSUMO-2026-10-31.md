# FTrade — Optimización de consumo (31/10/2026)

Objetivo: bajar RAM, CPU y egress de red en Railway sin cambiar señales, indicadores, planes ni la experiencia del usuario.

## Backend

1. **Hub compartido de eventos** (`api/event_hub.py`, `api/routes/events.py`). Antes cada navegador abría 7 conexiones Redis pub/sub propias y decodificaba cada mensaje. Ahora hay UNA suscripción por canal en todo el proceso y un único decode por mensaje. Con `REDIS_MAX_CONNECTIONS=32` el diseño anterior se agotaba con ~4 usuarios simultáneos. Las suscripciones se reintentan solas si Redis parpadea.
2. **Frames de precios en lote** (opt-in con `?batch=1`). En lugar de ~400 frames/s por navegador (uno por símbolo) se envía 1 frame por snapshot, omitiendo símbolos sin cambios para ese cliente. Los clientes antiguos siguen recibiendo el formato anterior.
3. **Pausa de precios con la pestaña oculta**: el cliente puede enviar `{"op":"prices","enabled":false}`; señales, alertas y eventos de cuenta siguen llegando.
4. **Sin consulta a Mongo por cada evento**: la fila de usuario se cachea por conexión (15 s, o al instante ante eventos de cuenta/pago/notificación de ese usuario). El acceso (trial/plan/ban) se recalcula en cada evento con esa fila, por lo que el vencimiento sigue siendo exacto.
5. **Búsqueda de señal compartida**: antes cada cliente ejecutaba `list_signals(limit=2000)` por evento; ahora se resuelve una vez por evento y se comparte.
6. **Resumen de liquidaciones** limitado a 1 frame / 3 s por conexión.
7. **Compresión gzip** (`GZipMiddleware`, nivel 4, `GZIP_LEVEL` configurable) para respuestas REST (p. ej. `/market/quotes`, ~400 KB de JSON repetitivo).
8. **Memoria de workers**: `pandas`/`numpy` ya no se cargan en 5 de los 8 workers (solo `signal_scanner` los necesita), ahorrando ~100 MB por proceso. `MALLOC_ARENA_MAX=2` en ambos Dockerfile reduce la fragmentación de glibc.

## Frontend

- Conexión WebSocket con `batch=1` y pausa/reanudación según visibilidad de pestaña.
- El respaldo REST de cotizaciones ya no se dispara porque un par ilíquido no tenga ticks: solo si todo el stream calla, o si un par líquido (top 40 por volumen) se detiene, máximo cada 10 s. Antes casi todo navegador visible descargaba la lista completa cada 2 s.
- Los eventos de liquidaciones/movers refrescan cotizaciones como máximo cada 15 s.
- Las consultas periódicas (señales, watchlist, mercado, régimen) no se ejecutan con la pestaña oculta.

## Compatibilidad / despliegue

Backend y frontend pueden desplegarse en cualquier orden (el protocolo nuevo es opt-in y los mensajes de control desconocidos se ignoran). No hay variables nuevas obligatorias; opcional: `GZIP_LEVEL` (1-9).

## Verificación realizada

- Compilación de todo el backend y prueba del hub (una suscripción por canal, aislamiento de listeners con error, liberación al desconectar el último cliente).
- No se pudo ejecutar `pytest` ni compilar el frontend en el entorno de trabajo (sin acceso a PyPI/npm). Antes de desplegar: `pytest -q` en el backend y `npm run build` en el frontend.

## Siguiente fase (no aplicada, requiere medir en producción)

- Mantener el mapa de cotizaciones en memoria del worker de mercado en vez de GET+SET completo cada segundo.
- Ver en Railway qué servicio produce los picos de RAM (métricas por servicio) antes de ajustar más.
