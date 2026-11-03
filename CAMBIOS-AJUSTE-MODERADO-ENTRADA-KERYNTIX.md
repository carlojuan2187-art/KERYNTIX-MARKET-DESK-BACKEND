# Ajuste moderado de entrada — KERYNTIX MARKET DESK

## Objetivo
Mejorar ligeramente el precio de entrada modelado sin añadir filtros de señal ni modificar la estrategia estructural.

## Cambios
- `LSR_PULLBACK_RETRACE_FRACTION`: valor predeterminado de `0.38` a `0.42`.
- `LSR_PULLBACK_ATR_MIN`: valor predeterminado de `0.22` a `0.24` ATR.

La entrada espera un retroceso apenas más profundo antes de activar la zona. Esto puede dar un precio de entrada más favorable y mejorar la relación R de las operaciones que se ejecuten. También puede reducir la cantidad de operaciones ejecutadas si el precio no vuelve a la zona. No garantiza elevar la tasa de aciertos ni la rentabilidad.

## Se conserva intacto
- Los filtros de referencia PLUS y los ajustes LONG/SHORT.
- Detección de liquidez, confirmación 5M y estructura 15M/1H.
- Stop loss estructural dinámico y objetivos estructurales dinámicos.
- Medición de resultados en R y cálculo de SL por riesgo individual.
- API, autenticación, notificaciones y frontend.

## Variables de Railway
Como las variables de entorno tienen prioridad sobre los valores predeterminados del código, comprobar el backend en Railway. Si existen estas variables, establecer:

```text
LSR_PULLBACK_RETRACE_FRACTION=0.42
LSR_PULLBACK_ATR_MIN=0.24
```

No crear ni cambiar otras variables de estrategia para esta calibración.

## Validación
- Suite completa backend: 410 aprobadas, 5 omitidas.
- Las 34 pruebas específicas de filtros PLUS, ajuste de entrada, resultados R y rendimiento pasaron.
- Las 5 omitidas dependen de MongoDB/Redis de integración o `python-telegram-bot`, no instalados en el entorno.

## Nota de evaluación
El resultado informado de 9 ganadas y 13 perdidas equivale a una tasa de acierto de 40,9% en 22 operaciones. Esta muestra por sí sola no valida una mejora. Comparar la siguiente muestra en demo mediante R neto acumulado, promedio R por operación, R medio de ganadoras y perdedoras, número de entradas ejecutadas y resultados por símbolo/dirección. No afirmar una mejora estadística antes de reunir más operaciones comparables.
