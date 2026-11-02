# KERYNTIX MARKET DESK — Autenticación con contraseña

## Endpoints

- `POST /auth/register`: crea un desafío de registro, guarda temporalmente el hash de la contraseña y exige vinculación/verificación por Telegram.
- `POST /auth/login`: autentica directamente mediante teléfono + contraseña y crea una sesión normal; no consulta Telegram.
- `POST /auth/recover`: inicia verificación por Telegram para una cuenta existente.
- `POST /auth/password`: actualiza la contraseña con una sesión autenticada tras la verificación de recuperación.
- `POST /auth/verify`: consume el código y, según el flujo, crea la cuenta o autentica la cuenta existente.

## Protección

- Las contraseñas se derivan con PBKDF2-HMAC-SHA256, salt aleatorio de 16 bytes y 310.000 iteraciones.
- El hash nunca se incluye en modelos de respuesta pública.
- El login aplica límites de intentos por teléfono e IP; pueden ajustarse con `PASSWORD_LOGIN_PHONE_LIMIT` y `PASSWORD_LOGIN_IP_LIMIT`.
- El backend devuelve `409 PASSWORD_SETUP_REQUIRED` para cuentas antiguas que todavía no tienen contraseña.

## Migración de usuarios existentes

No se asigna una contraseña automática ni se convierte el teléfono en contraseña. El usuario debe demostrar control de su Telegram vinculado mediante el flujo `/auth/recover` y `/auth/verify`, y luego guardar su contraseña con `/auth/password`. Tras esa configuración puede usar `/auth/login` sin Telegram.
