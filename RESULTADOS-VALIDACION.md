# Resultados de validación — Fase 23

## Comprobado en esta sesión

- Se revisó la estructura del paquete acumulado de fase 22 y las dependencias declaradas.
- Se separaron las dependencias de producción y de pruebas.
- Se preparó una imagen Docker de runtime basada en Python 3.11 y un target separado para test.
- Se añadieron tests de integración con guardia explícita `FTRADE_INTEGRATION=1` para no ejecutarlos accidentalmente en la suite normal.
- Se añadió Compose con MongoDB/Redis y un workflow CI usando Python 3.11.

## No comprobado desde esta sesión

- **No se construyó la imagen Docker**: Docker no está instalado/disponible.
- **No se ejecutaron tests contra MongoDB ni Redis reales**: necesitan los servicios del Compose o endpoints desechables.
- **No se ejecutó Python 3.11**: el intérprete disponible es Python 3.13.5.
- No se probaron Binance, BNB RPC, Telegram ni Web Push reales.

## Cómo obtener evidencia ejecutada

1. Ejecutar `docker compose -f docker-compose.integration.yml up --build --abort-on-container-exit backend-integration-tests` desde `backend/`.
2. Ejecutar `docker build --target runtime -t keryntix-market-desk-backend:local .`.
3. Ejecutar la suite completa del workflow `.github/workflows/backend-ci.yml` después de mover `backend/` a la raíz del repositorio backend.

No se publican conteos de pruebas aprobadas en esta fase porque no se pudieron ejecutar en este entorno las nuevas pruebas de infraestructura ni el runner Python 3.11.
