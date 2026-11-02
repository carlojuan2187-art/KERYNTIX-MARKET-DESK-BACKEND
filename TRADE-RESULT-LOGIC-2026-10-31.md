# KERYNTIX MARKET DESK — metodología de resultados 2.0

## Modelo de entrada y riesgo

- `entry_price` es la entrada modelada canónica de la estrategia.
- `entry_zone` es únicamente la ventana que activa el seguimiento de una señal. El precio real observado al tocar la zona se conserva aparte en `touch_price`.
- La distancia de riesgo es `abs(entry_price - stop_loss)`.
- Un Stop Loss alcanzado antes de TP1 equivale a `-1R`.
- TP1 y TP2 se expresan como la distancia desde `entry_price` hasta cada objetivo dividida por la misma distancia de riesgo.

## Protección progresiva

- Al alcanzar TP1, el estado pasa a `TP1_HIT` y se activa un stop simulado en el precio de TP1.
- Si el precio continúa hasta TP2 antes de volver a TP1, se cierra en TP2 y se registra `R(TP2)`.
- Si después de TP1 el precio vuelve al nivel de TP1, se cierra en TP1 y se registra `R(TP1)`.
- En señales de un solo objetivo, alcanzar TP1 cierra la operación.
- Si una vela de un minuto contiene movimientos incompatibles cuyo orden no se puede reconstruir (por ejemplo, TP2 y regreso a TP1 en la misma vela), se resuelve conservadoramente a TP1 y se marca `ambiguous`.

## Modelo de ejecución y limitaciones

- Se simula una posición completa, sin cierres parciales.
- No se modelan comisiones, financiación, spread ni deslizamiento.
- `exit_price` es el nivel simulado de salida, no un fill confirmado por un exchange.
- El feed WebSocket se evalúa en cada ciclo del worker (`SIGNAL_MONITOR_INTERVAL_SECONDS`, por defecto 2 s), usando solo cotizaciones frescas hasta `OUTCOME_LIVE_PRICE_MAX_AGE_SECONDS` (por defecto 5 s).
- Las velas cerradas de 1 minuto son el mecanismo de recuperación; se revisan según `OUTCOME_CHECK_SECONDS` (por defecto 20 s).
- No se promete ejecución ni detección en milisegundos. En modo de precio en vivo, la detección depende del ciclo del worker y de la frecuencia de actualización del feed; el respaldo por velas puede tardar hasta el siguiente cierre/revisión de vela.

## Compatibilidad histórica

- Las filas nuevas llevan `calculation_version=2.0` y `execution_model=SIMULATED_FULL_POSITION_TP1_PROTECTION`.
- Los resultados históricos no se recalculan ni reescriben. Se identifican como `legacy_v1` en el agregado de Resultados.
