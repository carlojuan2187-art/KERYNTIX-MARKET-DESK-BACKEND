# KERYNTIX MARKET DESK — Calibración moderada de confirmación v4.6

## Objetivo
Elevar ligeramente la calidad de confirmación para intentar mejorar el win rate sin añadir módulos, filtros nuevos ni reducir deliberadamente el flujo de señales con umbrales amplios. Esta es una hipótesis de calibración; no garantiza una mejora antes de validar nuevas operaciones.

## Cambios únicos
- `LSR_PLUS_MIN_CONFIRM_REL_VOLUME`: 0.82 → 0.84.
- `LSR_PLUS_MIN_CLOSE_POSITION`: 0.53 → 0.55.

Valores efectivos tras los ajustes direccionales existentes:
- SHORT: volumen de confirmación 0.84; posición de cierre 0.55.
- LONG: volumen de confirmación 0.91; posición de cierre 0.60.

## Se conserva sin cambios
- Filtros restantes del perfil PLUS.
- Umbral de puntuación SHORT 76 y LONG 78.
- Entrada dinámica y pullback 0.42 / 0.24 ATR.
- Reentrada tras SL.
- SL estructural dinámico y TP1/TP2 estructurales.
- Gestión TP1 protegido, TP2 y contabilización de un único R por operación.
- Cálculo de R y agregación de rendimiento.
- API, autenticación, pagos, usuarios, notificaciones y frontend.

## Variables Railway
Si existen overrides, actualizar únicamente:
```env
LSR_PLUS_MIN_CONFIRM_REL_VOLUME=0.84
LSR_PLUS_MIN_CLOSE_POSITION=0.55
```
No borrar las demás variables.

## Validación
El cambio se limita a dos umbrales de confirmación y sus expectativas de prueba. Los resultados futuros deben compararse por win rate, R acumulado, R medio, volumen de señales y desglose LONG/SHORT. La muestra comunicada (18 operaciones) sigue siendo pequeña y no permite afirmar una ventaja estadística estable.
