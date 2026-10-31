# FTrade — Corrección del histórico de funding

Fecha: 2026-10-31

## Causa

El servicio de analítica solicitaba el histórico de funding USDⓈ-M a `/futures/data/fundingRate`, mientras que el cliente REST público solo admite la ruta oficial `/fapi/v1/fundingRate`. El cliente rechazaba la primera ruta por no estar permitida; el servicio absorbía esa excepción como una fuente no disponible y la API devolvía `funding_history` vacío. Por eso la tasa actual podía aparecer, pero no la gráfica histórica.

## Cambio aplicado

- `api/services/analytics.py`: la consulta de `funding_history` ahora utiliza `GET /fapi/v1/fundingRate` con `symbol` y `limit=25`.
- Se conserva el parser de `fundingRate` y `fundingTime`; no se generan ni interpolan valores artificiales.
- No se requiere API key privada. El endpoint es de datos públicos de mercado.

## Pruebas

- `test/test_market_analytics_api.py`: comprueba la ruta exacta, parámetros y ausencia de la ruta anterior.
- `test/test_binance_rest_client.py`: comprueba que el cliente público permite la ruta oficial y procesa una respuesta de ejemplo.
- Suite backend completa: 350 aprobadas, 5 omitidas por requerir MongoDB/Redis reales o una dependencia opcional de Telegram.

## Despliegue

Desplegar este backend. No hay cambios frontend para este arreglo. La analítica se cachea durante 60 segundos (`DERIVATIVES_TTL`), por lo que después del despliegue puede ser necesario esperar a que expire el snapshot anterior o invalidar `market:derivatives:v1:<SYMBOL>` para ver el histórico actualizado inmediatamente.
