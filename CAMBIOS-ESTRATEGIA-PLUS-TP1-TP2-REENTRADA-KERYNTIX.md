# KERYNTIX MARKET DESK — calibración PLUS, TP1/TP2 y reentrada

## Alcance

Cambios limitados al motor de señales y su seguimiento postentrada, a las estadísticas R y a los textos de ayuda/estado que explican el nuevo comportamiento. No se modificaron autenticación, pagos, notificaciones, suscripciones ni la interfaz de administración.

## Estrategia

- Se conserva el perfil de filtros PLUS que ya estaba incorporado desde `strategy_liquidity_sweep.py`, incluidos los valores direccionales LONG/SHORT.
- Se conserva el ajuste de entrada moderado existente: `LSR_PULLBACK_RETRACE_FRACTION=0.42` y `LSR_PULLBACK_ATR_MIN=0.24`.
- TP1 conserva su ventana estructural original.
- TP2 busca niveles históricos estructurales en una ventana ampliada (por defecto 2x). Solo se publica si existe un swing/pool real por delante de la entrada, dentro del alcance ATR configurado y separado de TP1 por al menos `TARGET_MIN_GAP_ATR`. No se crea un TP2 sintético de 1.5R/2R.
- Si no existe un segundo nivel estructural razonable, la señal conserva un único TP1.
- Después de un SL, una reentrada en el mismo símbolo y dirección requiere un nuevo barrido de liquidez confirmado después de la hora de cierre del SL. No bloquea otras direcciones ni otros símbolos.

Variables documentadas en `.env.example`:

```dotenv
LSR_PULLBACK_RETRACE_FRACTION=0.42
LSR_PULLBACK_ATR_MIN=0.24
LSR_TARGET_TP2_LOOKBACK_MULTIPLIER=2.0
LSR_REQUIRE_FRESH_SWEEP_AFTER_SL=true
```

## Seguimiento y contabilización

- Antes de TP1, permanece activo el SL dinámico y estructural de la señal.
- Al tocar TP1, la operación no se cierra si tiene TP2; pasa a `TP1_HIT` y la protección se sitúa en el precio de entrada modelado.
- Si después vuelve a la entrada, se cierra como `TP1` y se acredita exclusivamente `r_tp1`.
- Si continúa y toca TP2, se cierra como `TP2` y se acredita exclusivamente `r_tp2`. Nunca se suman TP1 y TP2 en una misma operación.
- Las señales que solo tienen TP1 siguen cerrándose al tocarlo.
- Si una vela de 1 minuto contiene tanto TP2 como el retorno a la entrada, el orden no puede conocerse con OHLC; se resuelve conservadoramente como TP1 y queda marcada como ambigua.
- Las operaciones que ya estaban en `TP1_HIT` bajo la versión anterior migran su precio de protección a la entrada. Las señales históricas `TP1_HIT` que solo contienen un objetivo se liquidan como TP1 para evitar que queden abiertas sin objetivo de continuación.
- El SL se calcula usando la distancia de riesgo propia de cada señal (entrada a su SL dinámico) y el precio de salida simulado. Una ejecución exactamente en el nivel SL equivale a −1R por definición de R; un gap que atraviese el stop puede ser peor que −1R.
- La metodología de resultados pasa a la versión `2.3`; se conserva el soporte de snapshots anteriores `2.0`, `2.1` y `2.2`. El modelo de objetivos se identifica como `structural_liquidity_v2_extended_tp2`.

## Archivos modificados — backend

```text
.env.example
engine/liquidity_strategy.py
engine/outcome_logic.py
engine/performance.py
worker/signal_scanner.py
test/test_lifecycle_worker.py
test/test_outcome_logic.py
test/test_performance_api.py
```

## Archivos nuevos — backend

```text
test/test_signal_scanner_reentry.py
test/test_structural_tp2.py
CAMBIOS-ESTRATEGIA-PLUS-TP1-TP2-REENTRADA-KERYNTIX.md
```

## Archivos modificados — frontend

```text
src/assets/guide.json
src/i18n/translation.ts
src/pages/Resultados.tsx
src/pages/Signals.tsx
```

Los textos de la guía y las pantallas de seguimiento ahora explican que TP1 protege en la entrada y que el resultado final contabiliza TP1 o TP2, nunca la suma.

## Validación

- 64 pruebas enfocadas a filtros PLUS, calibración de entrada, generación estructural de TP2, reentrada tras SL, seguimiento TP1/TP2, R, rendimiento y ciclo de vida: aprobadas.
- Suite completa excluyendo una prueba WebSocket intermitente: 417 aprobadas, 5 omitidas.
- La prueba WebSocket excluida pasó al ejecutarse individualmente, pero ha presentado `CancelledError` de forma intermitente al ejecutarse dentro de la suite completa.
- `py_compile` de los módulos backend modificados: correcto.
- Transpilación sintáctica TypeScript/TSX de los archivos frontend modificados y parseo de `guide.json`: correctos.
- No se completó `vite build` porque este entorno no tiene instaladas las dependencias de frontend (`node_modules`).

## Limitaciones

Estos cambios implementan una nueva gestión y calibración; no prueban por sí mismos que aumente la rentabilidad. Hace falta un backtest reproducible y una validación demo posterior con suficientes operaciones. El seguimiento sigue siendo una simulación y no incorpora comisiones, funding, spread ni slippage real del broker.
