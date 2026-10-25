# FTrade Web — Fase 23: entorno reproducible e integración de infraestructura

Esta fase prepara el entorno para verificar el backend con **Python 3.11** y con **MongoDB y Redis reales**, sin desplegarlo todavía. El frontend se conserva y no se cambia su diseño.

## Entregables

- `backend/Dockerfile`: imagen para ejecución y target específico de pruebas.
- `backend/requirements-prod.txt` y `backend/requirements-dev.txt`: separación de dependencias runtime y test.
- `backend/docker-compose.integration.yml`: stack local desechable MongoDB + Redis + pruebas.
- `backend/tests/integration/`: tests contra servicios reales, omitidos en la suite unitaria normal.
- `backend/.github/workflows/backend-ci.yml`: CI con Python 3.11 y contenedores Mongo/Redis efímeros.
- `API_CONTRACT.md`: copia versionada del contrato que sirve como referencia autónoma al mover el backend a su propio repositorio.
- `backend/CONTRATO-DE-FASE.md`: instrucciones, comandos y limitaciones.
- `backend/MATRIZ-COBERTURA.md`: mapa de cobertura de esta fase.

## Ejecutar

Desde `backend/`, con Docker instalado:

```bash
docker compose -f docker-compose.integration.yml up --build --abort-on-container-exit backend-integration-tests
```

Build de runtime:

```bash
docker build --target runtime -t ftrade-backend:local .
```

Si el proyecto backend se mueve al repositorio independiente acordado, el contenido de la carpeta `backend/` debe ser la raíz de ese repositorio. En ese momento GitHub reconocerá automáticamente `backend/.github/workflows/backend-ci.yml` como `.github/workflows/backend-ci.yml`.

## Validación de esta entrega

Se revisó la sintaxis de los nuevos archivos Python, la estructura de la configuración y los comandos. **No se afirma haber ejecutado Docker ni Python 3.11**: este entorno solo trae Python 3.13.5 y no tiene Docker CLI/daemon. Por eso las pruebas contra servicios reales están preparadas para ejecutarse en Docker Compose o GitHub Actions, y la validación de los contenedores permanece pendiente.

Esta fase no conecta aún todos los componentes del frontend y no constituye una aprobación para producción.
