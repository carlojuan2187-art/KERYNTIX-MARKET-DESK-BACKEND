# Matriz de cobertura — Fase 23

| Área | Estado en esta fase | Evidencia | Límite |
|---|---|---|---|
| Runtime Python 3.11 | Preparado | Dockerfile `python:3.11-slim-bookworm` y CI | No ejecutable en el contenedor actual |
| Dependencias de producción | Separadas | `requirements-prod.txt` | Deben resolverse durante build en entorno con Internet |
| Dependencias de pruebas | Separadas | `requirements-dev.txt` | La suite local completa depende de instalarlo |
| MongoDB real | Pruebas preparadas | índices únicos y claim de transacción | Requiere Docker o Mongo URI desechable |
| Redis real | Pruebas preparadas | JSON roundtrip, ping y scripts CAS de leases | Requiere Redis disponible |
| Docker runtime | Preparado | target runtime, usuario no root y healthcheck | No se pudo construir porque Docker CLI/daemon no está disponible aquí |
| CI backend | Preparado | workflow Python 3.11 + servicios Mongo/Redis | No ejecutado desde esta sesión; se ejecuta en GitHub |
| Diseño del frontend | Sin cambios visuales | no se editaron componentes, estilos ni assets | el build de Vite sigue pendiente en su propio CI |
| Integraciones externas | Pendiente | no consume credenciales reales | validar Binance, BNB RPC, Telegram y Web Push en staging |
