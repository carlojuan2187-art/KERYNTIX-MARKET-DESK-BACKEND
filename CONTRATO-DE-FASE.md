# FTrade Web — Fase 23: entorno reproducible y pruebas de integración

## Objetivo

Resolver la brecha entre pruebas unitarias y validación de infraestructura. Esta fase prepara una imagen de backend reproducible en Python 3.11 y un proceso automatizado para probar MongoDB y Redis reales en entornos desechables. No despliega servicios, no usa credenciales reales y no modifica el diseño visual del frontend.

## Incluye

- `backend/Dockerfile`: imagen de runtime Python 3.11 sin pytest ni código de tests; target `test` separado para CI.
- `backend/requirements-prod.txt`: solo dependencias de ejecución.
- `backend/requirements-dev.txt`: dependencias de ejecución más pytest.
- `backend/docker-compose.integration.yml`: MongoDB y Redis aislados, enlazados a loopback del host y una tarea desechable de pruebas.
- `backend/tests/integration/test_live_infrastructure.py`: ejercicios reales de ping, persistencia, índices únicos, idempotencia de claims y leases de Redis. Se omiten en la suite normal si `FTRADE_INTEGRATION` no es `1`.
- `backend/.github/workflows/backend-ci.yml`: CI de Python 3.11 para unit tests, compilación, release preflight e integración contra servicios efímeros.

## Ejecutar localmente con Docker Compose

Desde `backend/`:

```bash
docker compose -f docker-compose.integration.yml up --build --abort-on-container-exit backend-integration-tests
```

Para cerrar y eliminar los contenedores:

```bash
docker compose -f docker-compose.integration.yml down --remove-orphans
```

La base de datos de integración está aislada (`ftrade_web_integration`). No apuntar esta configuración a la base de datos del bot ni a datos de producción. MongoDB y Redis de este Compose no tienen autenticación y solo publican puertos en `127.0.0.1`, por lo que el archivo es exclusivamente local/desarrollo.

## Construcción de la imagen de runtime

Desde `backend/`:

```bash
docker build --target runtime -t ftrade-backend:local .
```

La imagen expone el puerto `8000` por defecto, acepta `PORT` y tiene un healthcheck HTTP sobre `/live`. En Railway, configura el healthcheck de readiness por separado si quieres bloquear tráfico hasta que MongoDB/Redis y los procesos necesarios estén listos.

## Pruebas

Suite normal:

```bash
python -m pip install -r requirements-dev.txt
PYTHONPATH=. python -m pytest -q --disable-warnings
PYTHONPATH=. python -m compileall -q api domain engine worker main.py scripts tests
PYTHONPATH=. python -m scripts.release_preflight --json
```

Pruebas de infraestructura directamente contra servicios ya levantados:

```bash
FTRADE_INTEGRATION=1 MONGODB_URI='mongodb://127.0.0.1:27018/?directConnection=true' \
DATABASE_NAME='ftrade_web_integration' REDIS_URL='redis://127.0.0.1:6380/0' \
FTRADE_DEPLOYMENT_NAME='manual_integration' \
PAYMENT_TXS_COLLECTION='integration_payment_txs' \
PAYMENTS_COLLECTION='integration_payments' REFERRALS_COLLECTION='integration_referrals' \
REFERRAL_REWARDS_COLLECTION='integration_referral_rewards' SUPPORT_TICKETS_COLLECTION='integration_support_tickets' \
ADMIN_AUDIT_COLLECTION='integration_admin_audit' \
PYTHONPATH=. python -m pytest -q tests/integration
```

## Límites conocidos

- El entorno de esta sesión no dispone de Docker ni de Python 3.11, por tanto no es posible ejecutar aquí la integración contra los contenedores ni certificar la imagen. El workflow lo hará al ejecutarse en GitHub Actions o puede correrse localmente en un equipo con Docker.
- Los tests de integración cubren adaptadores de infraestructura, no todo el recorrido de negocio contra un conjunto completo de servicios externos.
- Binance, RPC de BNB Smart Chain, Telegram y Web Push siguen necesitando una prueba de staging con credenciales no productivas.
- El workflow queda bajo `backend/.github/workflows/` porque la arquitectura acordada mantiene un repositorio independiente para el backend. Al crear ese repositorio, el contenido de `backend/` pasa a ser la raíz del repo y GitHub reconocerá el workflow.
