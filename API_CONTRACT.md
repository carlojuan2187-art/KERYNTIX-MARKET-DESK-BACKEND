# Contrato API De FTrade Web

## Alcance

La aplicación conserva un **modo maqueta** para revisar el diseño, y añade un modo `VITE_AUTH_MODE=api` que conecta a FastAPI la autenticación, el perfil, las restricciones, la vinculación de Telegram y las sesiones. Los módulos analíticos no se consideran conectados por activar ese modo: siguen usando mocks hasta sus fases de integración.

Los modelos autoritativos son `src/api/types.ts`, `src/api/terminal.ts`, `src/api/account.ts` y los tipos `CalcInput` / `CalcResult` de `src/api/index.ts`. No se deben renombrar los campos del usuario, estados ni razones de cancelación.

Base propuesta: `${VITE_API_URL}`. JSON UTF-8, fechas ISO 8601 en UTC, importes numéricos en USDT salvo indicación distinta. Los periodos estadísticos se delimitan en UTC; la presentación usa `user.tz` (America/Havana por defecto).

## Autenticación Y Errores

Modo API: `src/api/client.ts` envía `Authorization: Bearer <access_token>` de vida corta; el access token permanece en memoria, mientras el refresh token queda exclusivamente en cookie `HttpOnly; Secure; SameSite=Lax` manejada por el backend. El navegador usa `credentials: include`, restaura sesión mediante `POST /auth/refresh` y rota el token automáticamente tras un 401 en rutas protegidas. Nunca devolver el refresh token en JSON ni guardarlo en localStorage.

Modo maqueta: el acceso Premium/admin inicial y el código `123456` son únicamente recursos de demostración. La interfaz los oculta al activar `VITE_AUTH_MODE=api`. No publicar el modo `mock` como una aplicación autenticada de producción.

Error común: `{ code: string, message: string, details?: object }`. Todas las rutas autenticadas pueden responder `401 UNAUTHENTICATED`, `403 BANNED`, `429 TOO_MANY_REQUESTS` o `500 INTERNAL_ERROR`. Las rutas de análisis añaden `403 ACCESS_EXPIRED`; las administrativas, `403 ADMIN_REQUIRED`. `403 BANNED` incluye `reason` y `ban_until` cuando corresponda.

Regla canónica de acceso: el servidor evalúa `domain/plans.py` con el registro actual y el momento actual en UTC: administrador desde `ADMIN_USER_IDS` o mediante el teléfono verificado configurado en `ADMIN_PHONE_NUMBERS`; si no, baneo permanente o temporal aún vigente bloquea; de lo contrario, Premium vigente o prueba vigente permite análisis. Un baneo temporal deja de ser efectivo cuando `ban_until` queda atrás aunque `banned=true` siga guardado. `/account/plan` y `/account/entitlements` permiten al usuario consultar cómo recuperar acceso tras vencer; los endpoints analíticos siguen bloqueados. La pantalla de bloqueo es global.

## Endpoints

### Acceso, Cuenta Y Sesiones

| Método y ruta                        | Request                                                 | Response 200                                                  | Errores adicionales                          |
| ------------------------------------ | ------------------------------------------------------- | ------------------------------------------------------------- | -------------------------------------------- |
| `POST /auth/register`                | `{phone,password,ref_code?}`; teléfono E.164 y contraseña de 10–128 caracteres | `{token,phone,step:"awaiting_telegram",expires_at,resend_at}` | 409 teléfono registrado; 422 input; 429 frecuencia |
| `POST /auth/login`                   | `{phone,password}`; teléfono E.164                      | `{access_token,user:User,is_new:false}` + cookie refresh     | 401 credenciales inválidas; 409 `PASSWORD_SETUP_REQUIRED`; 429 intentos |
| `POST /auth/recover`                 | `{phone}` E.164; cuenta existente                        | `{token,phone,step:"awaiting_telegram",expires_at,resend_at}` | 404 cuenta; 429 frecuencia                  |
| `POST /auth/password`                | `{password}`; sesión verificada requerida                | `User` actualizado                                           | 401 sesión; 422 contraseña                   |
| `POST /auth/phone`                   | `{phone}` E.164; compatibilidad con clientes antiguos    | `{token,phone,step:"awaiting_telegram",expires_at,resend_at}` | 422 teléfono inválido; 429 frecuencia        |
| `GET /auth/telegram-check?token=...` | Query `token`                                           | `{linked,telegram_chat_id?}`                                  | 404 solicitud; 410 vencida                   |
| `POST /auth/verify`                  | `{phone,code}`                                          | `{access_token,user:User,is_new}` + cookie refresh            | 422 código; 410 vencido; 429 tras 5 intentos |
| `POST /auth/resend`                  | `{token}`                                               | `{expires_at,resend_at}`                                      | 404 solicitud; 429 antes de 60s              |
| `POST /auth/refresh`                 | Cookie refresh                                          | `{access_token}` + cookie rotada                              | 401 sesión revocada                          |
| `POST /auth/logout`                  | Cookie refresh                                          | 204, elimina cookie                                           | 401 sesión inexistente                       |
| `GET /auth/me`                       | Sin body                                                | `User`                                                        | Errores comunes                              |
| `PATCH /auth/me`                     | Preferencias editables: `{exchange?,tz?,risk_profile?}` | `User` actualizado                                            | 422 zona IANA o perfil inválido              |
| `GET /account/restriction`           | Sin body                                                | `{reason,status,allowed,ban_active,ban_until?,access_until?}` | Permitido al usuario bloqueado               |
| `GET /account/plan`                  | Sin body                                                | Snapshot de plan, vigencia y estado efectivo                 | Errores comunes                              |
| `GET /account/entitlements`           | Sin body                                                | Snapshot más mapa `features`                                 | Errores comunes                              |
| `POST /account/telegram`             | Solicitud de vinculación                                | `{token,deep_link,expires_at}`                                | 409 contacto de otro usuario                 |
| `DELETE /account/telegram`           | Sin body                                                | `User` sin `telegram_chat_id`                                 | Errores comunes                              |
| `GET /sessions`                      | Sin body                                                | `SessionRecord[]`                                             | Errores comunes                              |
| `DELETE /sessions/:id`               | ID de sesión                                            | 204                                                           | 404 sesión ajena/inexistente                 |

`SessionRecord = {id,device,location,current,last_activity}`. En producción la asignación de `telegram_chat_id` debe ocurrir exclusivamente después de verificar el contacto en el backend. El cambio directo del campo en la demo no es un mecanismo de vinculación de producción.

El flujo de la web separa `Iniciar sesión` y `Crear cuenta`: el login normal utiliza teléfono + contraseña y no abre Telegram. El registro utiliza teléfono + contraseña y exige verificación por Telegram antes de crear la cuenta. La recuperación de contraseña también exige Telegram. Las cuentas creadas antes de incorporar contraseñas deben completar una única configuración mediante `POST /auth/recover`, `POST /auth/verify` y `POST /auth/password`; mientras no tengan una contraseña configurada, `POST /auth/login` responde `409 PASSWORD_SETUP_REQUIRED`. Las contraseñas se almacenan como hash PBKDF2-HMAC-SHA256 con salt individual; nunca se devuelven en las respuestas.

El enlace del bot tiene la forma `https://t.me/<BOT_USERNAME>?start=verify_<token>`. No se usa Telegram Login Widget ni interfaces de chat. El botón de simulación no corresponde a un endpoint de producción.

Un nuevo registro recibe `plan:"free"`, `trial_end = now + TRIAL_DAYS`, preferencias de avisos activas y `tz:"America/Havana"`. `is_new` activa el aviso de bienvenida solo en ese registro. El frontend conserva temporalmente un código de referido de `/r/ref_<user_id>`; el backend debe resolverlo y rechazar auto-referidos.

`User` conserva exactamente: `user_id, phone, telegram_chat_id, username, plan, trial_end, plan_end, ref_code, referred_by, ref_premium_valid, exchange, risk_profile, watchlist, alert_prefs, tz, banned, ban_until, banned_at, banned_by, created_at, last_activity`. `is_admin` es un indicador adicional de autorización de la sesión, establecido solo por el servidor.

`risk_profile = {capital:number,risk_pct:number}` donde **1 significa 1%**, no 0,01. `alert_prefs = {on,liq,mov,vol,signals,price,plans,in_app,telegram}` con valores booleanos. En la persistencia la watchlist es una lista de símbolos normalizados (máximo 10); el endpoint responde con un objeto enriquecido `{items,total,max_items,status,source,updated_at,analysis_status}`. El servidor no debe permitir que PATCH de perfil modifique plan, baneo, rol o referidos.

### Panel, Catálogo Y Gráficos

| Método y ruta                                 | Request                   | Response 200                | Errores adicionales        |
| --------------------------------------------- | ------------------------- | --------------------------- | -------------------------- |
| `GET /overview`                               | Sin body                  | `OverviewData`              | 403 acceso vencido         |
| `GET /market/quotes`                          | Sin body                  | `AssetQuote[]`              | 503 datos no disponibles   |
| `GET /symbols?query=...`                      | Nombre, prefijo o símbolo | `AssetQuote[]` coincidentes | 422 query inválida         |
| `GET /symbols/:symbol`                        | Símbolo normalizado       | `AssetQuote`                | 404 par no válido          |
| `GET /market/candles?symbol=...&timeframe=1h` | `1m,5m,15m,1h,4h,1D`            | `Candle[]` cronológico      | 404 símbolo; 422 timeframe |

`AssetQuote = {symbol,name,price,change_pct,volume_24h,high_24h,low_24h,funding_rate,open_interest,buy_pct,volume_ratio,score,trend:{"15M","1H","4H"},sparkline:number[],liquidity_sweep}`. Tendencias: `"up"|"down"|"flat"`. `change_pct` está en unidades porcentuales; `funding_rate` es una fracción (0.0001 = 0.01%). OI y volumen están en USDT.

`Candle = {time,timestamp,open,high,low,close,volume,cvd}`. `timestamp` usa milisegundos Unix; `time` es la etiqueta de presentación, calculada con la zona del usuario. Volumen y CVD del mock están en USDT.

`OverviewData = {quotes,signals,regime,performance,liquidations,alerts,radar,breadth:{up,down,total},updated_at}`. Las variantes Terminal y Panorama consumen este mismo snapshot; no generan señales ni cálculos distintos.

### Señales, Radar Y Watchlist

| Método y ruta               | Request          | Response 200                        | Errores adicionales                |
| --------------------------- | ---------------- | ----------------------------------- | ---------------------------------- |
| `GET /signals`              | Sin body         | `SignalState[]`, máximo 10          | 403 acceso vencido                 |
| `GET /signals/:symbol`      | Símbolo          | `SignalState` completa más reciente | 404 señal                          |
| `GET /radar`                | Sin body         | `RadarOpportunity[]` priorizado     | 503 scanner no disponible          |
| `GET /watchlist`            | Sin body         | `WatchlistResponse` con `items`, análisis 15M/1H/4H y score | 403 acceso vencido; 503 mercado no verificable |
| `POST /watchlist`           | `{symbol}`       | `WatchlistResponse` actualizada     | 422 símbolo; 409 duplicado o máximo 10 |
| `DELETE /watchlist/{symbol}` | Símbolo         | `WatchlistResponse` actualizada     | 404 elemento                       |

Estados de señal, sin añadir otros: `PENDING|TOUCHED|CANCELLED`. Seguimiento separado en `operation_result`: `OPEN|TP1_HIT|TP1|TP2|SL`. `event = {type,result?,r?,via?}`.

La señal incluye símbolo, dirección, `entry_low,entry_high,entry_price,stop_loss,tp1,tp2,tp3?,score,score_breakdown,leverage,timeframes,created_at,expires_at?,touched_at?,cancelled_at?,cancel_reason?,operation_result?,r_result?`. Precios en números, sin notación científica al copiar.

`cancel_reason` conserva los siete valores: `ENTRY_NOT_TOUCHED`, `TP_REACHED_BEFORE_ENTRY`, `AMBIGUOUS_ENTRY_TP_ORDER`, `PRE_ENTRY_INVALIDATED`, `AMBIGUOUS_ENTRY_SL_ORDER`, `STALE_ENTRY_TOUCH`, `INVALID_GEOMETRY`.

PENDING dispone de 15 minutos. Las canceladas se conservan 30 minutos y no afectan a los resultados. Las entradas tocadas se muestran 30 minutos; el seguimiento OPEN/TP1_HIT continúa sin vencimiento hasta el cierre. El ejemplo mantiene el mismo objeto con `status:TOUCHED` para ese seguimiento.

`score_breakdown` usa `structure,confluence,momentum,liquidity`; máximos 30/30/20/20. El radar utiliza una lista plana con `kind:"setup"|"sweep"|"watch"|"move"` y `{symbol,label,direction,score,detail,score_breakdown,created_at}`. Esta es una decisión de estructura de transporte, no una nueva función del scanner.

Normalización: mayúsculas, quitar espacios y `/`, añadir USDT si falta, **sin quitar el prefijo 1000 ni letras del nombre**. El mock valida 17 pares de ejemplo; el backend debe validar con el catálogo real de Binance USDT-M.

### Análisis De Mercado

| Método y ruta | Request | Response 200 | Errores adicionales |
|---|---|---|---|
| `GET /market/regime` | Sin body | `MarketRegime` | 503 snapshot no disponible |
| `GET /derivatives?symbol=...` | Símbolo | `DerivativesData` | 404 par |
| `GET /calendar?view=7` | `view=7` o `30` | `CalendarEventItem[]` cronológico | 422 ventana |
| `GET /market/flow?symbol=...` | Símbolo | `FlowData` | 404 par; 503 recopilando |
| `GET /market/liquidations?window=15` | `window=5`, `15` o `60` | `LiquidationSummary` | 422 ventana; 503 recopilando |
| `GET /market/movers?window=15` | `window=5`, `15` o `60` | `MoversSnapshot` | 422 ventana; 503 recopilando |

`MarketRegime` y `DerivativesData` conservan los modelos de `src/api/types.ts`. La clasificación es una de `TENDENCIA ALCISTA|TENDENCIA BAJISTA|RANGO|TRANSICIÓN|ALTA VOLATILIDAD`. Régimen se refresca cada 5 minutos. Las proporciones de cuentas y top traders están en porcentajes; `mark_index_delta` es un porcentaje.

`CalendarEventItem = {id,title,date,impact:"alto"|"medio"|"bajo",category,countdown?}`. El cliente calcula la cuenta regresiva desde `date`. Deribit: viernes 08:00 UTC; mensual, último viernes del mes; trimestral en marzo/junio/septiembre/diciembre. La ventana 30 omite los semanales. Las fechas macro del mock son relativas y no reales.

`FlowData = {symbol,windows:[{label:"15m"|"1h"|"4h",buy_pct,delta,delta_usdt,price,change_pct}],cvd:Candle[],volume_ratio,reading}`. `delta` está en el activo; `delta_usdt`, en USDT; CVD muestra las últimas 4h.

`LiquidationSummary = {window,long_usdt,short_usdt,total,orders,pace,usdt_per_min,pairs:[{symbol,long_usdt,short_usdt}],largest_order:{symbol,side,amount_usdt,created_at},reading,updated_at}`.

`MoversSnapshot = {window,gainers:AssetQuote[],losers:AssetQuote[],volume_flat:AssetQuote[],btc_change_pct,breadth_pct,reading,updated_at}`. Top 5 por lado, con volumen diario mayor que 20 M USDT. `~` en la interfaz indica ratio de volumen estimado.

Para arranque en producción, responder 503 con `{code:"COLLECTING",message,collected_minutes,required_minutes}` hasta completar la ventana. No rellenar faltantes con ceros: debe mostrarse el estado de recopilación.

### Resultados Y Riesgo

| Método y ruta | Request | Response 200 | Errores adicionales |
|---|---|---|---|
| `GET /results?period=month` | `period=day`, `week`, `month` o `all` | `Performance` | 422 periodo |
| `POST /calculator` | `CalcInput` | `CalcResult` | 422 geometría, riesgo o precios inválidos |

`Performance = {operations,wins,losses,win_rate,total_r,average_r,best_r,worst_r,streak,curve:[{date,r}],trades:ClosedOperation[]}`. `ClosedOperation = {id,symbol,direction,entry_price,exit_price,result:"TP1"|"TP2"|"SL",r_result,touched_at,closed_at}`. Solo cierres con toque de entrada. TP1 seguido de stop se conserva como TP1; SL anterior a TP1 = -1R. Rachas positivas para ganadas consecutivas y negativas para pérdidas.

`CalcInput = {capital,risk_pct,entry,stop,tp1,tp2?,tp3?,direction?}`. La dirección se deduce del stop y no se acepta geometría incoherente. `risk_pct:1` equivale a 1%.

`CalcResult = {risk_usdt,qty,position_size,leverage_min,isolated_margin,liquidation_approx,fee_taker_pct,fees_usdt,direction,results:[{level,price,r,usdt,fees_usdt,net_usdt}]}`.

Decisiones explícitas de cálculo: riesgo = capital _ riesgo / 100; cantidad = riesgo / distancia al stop; tamaño = cantidad _ entrada; apalancamiento mínimo = ceil(tamaño / capital), mínimo 1; margen = tamaño / apalancamiento mínimo. Liquidación aproximada con MMR 0,4% y fórmula `entry * (1 - sign * (1/leverage - 0.004))`. Comisión taker 0,05% por lado, con precio de salida específico para cada TP. El riesgo del stop excluye comisiones; el neto de cada objetivo las descuenta. Se supone cierre completo en cada objetivo de forma independiente, no cierres parciales acumulados.

### Avisos Y Notificaciones

| Método y ruta | Request | Response | Errores adicionales |
|---|---|---|---|
| `GET /account/alert-preferences` | Sin body | `{preferences,updated_at}` | 401 sesión |
| `PATCH /account/alert-preferences` | Uno o más booleanos `on,liq,mov,vol,signals,price,plans,in_app,telegram,web_push` | Preferencias actualizadas | 422 claves/cuerpo vacío |
| `GET /alerts/price?enabled_only=false` | Sin body | Alertas de precio propias | 403 acceso vencido |
| `POST /alerts/price` | `{symbol,condition:"above"|"below",target_price,repeat?,cooldown_seconds?}` | Alerta creada | 409 duplicado activo; 422 input/símbolo |
| `PATCH /alerts/price/{alert_id}` | `{enabled?,repeat?,cooldown_seconds?}` | Alerta actualizada | 404 ajena/inexistente; 409 duplicado activo |
| `DELETE /alerts/price/{alert_id}` | ID | 204 | 404 ajena/inexistente |
| `GET /notifications?unread_only=false&limit=50&before=...` | Parámetros opcionales | `{items,unread_count,limit,next_before}` | 401 sesión |
| `POST /notifications/{id}/read` | ID | `{id,read_at,unread_count}` | 404 notificación |
| `POST /notifications/read-all` | Sin body | `{updated,unread_count}` | 401 sesión |
| `DELETE /notifications/{id}` | ID | 204 | 404 notificación |
| `GET /push/vapid-public-key` | Sin body; endpoint público | `{public_key,application_server_key}` | 503 VAPID no configurado |
| `GET /push/subscriptions` | Sesión actual | `{items,total,max_items}`; no expone endpoint ni claves | 401 sesión |
| `POST /push/subscriptions` | Objeto `subscription.toJSON()` con `endpoint`, `expirationTime?`, `keys.p256dh`, `keys.auth` | Metadata sanitizada de suscripción | 422 endpoint/keys inválidos o expirados; 409 límite de navegadores |
| `DELETE /push/subscriptions/{id}` | ID propio | 204 | 404 suscripción ajena/inexistente |

`AlertItem = {id,kind:"liq"|"mov"|"vol",key,text,symbol?,focus?,created_at}`. `AppNotification = {id,title,text,created_at,read,href}`. Abrir un aviso lo marca como leído sin borrarlo. Los eventos de señales y avisos se conservan en el centro.

Máximo 8 avisos de anomalías por hora y no repetir par en 45 minutos salvo magnitud duplicada. La demo usa magnitud fija y suprime todas las repeticiones dentro del plazo. Los avisos emergentes duran `VITE_ALERT_DURATION_SECONDS` (180 por defecto); los eventos de señales, 7s. Un usuario bloqueado o sin acceso no recibe avisos de mercado ni de señales.

La Notification API del navegador con la página abierta no equivale a Web Push. La entrega por Telegram y la cola Web Push del backend usan reintentos duraderos. La fase 20 incorpora las rutas de suscripción, VAPID, persistencia y worker de entrega; `web_push` está desactivado por defecto y requiere consentimiento explícito. La integración React/service worker y la prueba real de recepción con la web cerrada siguen pendientes. Los endpoints de suscripción aceptan solo HTTPS de los proveedores conocidos y la API de lista no expone el endpoint completo ni las claves.

### Pagos, Referidos Y Soporte

| Método y ruta           | Request                          | Response 200                | Errores adicionales                    |
| ----------------------- | -------------------------------- | --------------------------- | -------------------------------------- |
| `POST /payments/start`  | Sin monto elegido por el cliente | `PaymentSession`            | 409 sin monto único disponible         |
| `POST /payments/verify` | `{record_id,wallet_from}`        | `PaymentRecord` actualizado | 404 pago; 422 wallet; 410 vencido      |
| `GET /payments/history` | Sin body                         | `PaymentRecord[]`           | Errores comunes                        |
| `GET /referrals`        | Sin body                         | `ReferralSummary`           | Errores comunes                        |
| `GET /support/tickets`  | Sin body                         | `SupportTicket[]`           | Errores comunes                        |
| `POST /support/tickets` | `{subject,message}`              | `SupportTicket`             | 422 asunto <3 o mensaje <10 caracteres |

`PaymentSession = {price:string,wallet_dest:string,record:PaymentRecord,expires_at}`. `PaymentRecord = {id,amount_usdt:string,wallet_from?,status,created_at,updated_at?,confirmations?}`. Estados exactos: `pending|verifying|confirmed|expired|failed`. No usar `verified`.

Precio = PREMIUM_PRICE_USDT (40) + un sufijo de 0.001 a 0.099, único entre solicitudes activas. La ventana dura 180 minutos. El backend valida red BEP20, token USDT, monto, origen y destino, y espera 12 confirmaciones. Una confirmación ya aplicada es **idempotente**. `plan_end = max(now,plan_end activo) + 30 días`; la prueba no cambia. La demo suma 4 confirmaciones por consulta y no lee la cadena. Una wallet cero provoca fallo de ejemplo. El QR codifica únicamente la dirección de destino simulada; no debe usarse para transferir fondos.

`ReferralSummary = {link,progress:{current,target:5},referrals:[{user_id,username,created_at,activated_premium_at:null|string}],rewards:[{id,days,created_at}]}`. Solo una activación Premium suma al progreso. Cada grupo de 5 activa o amplía Premium 30 días. Dos activaciones no producen dos recompensas: el ejemplo no tiene ninguna obtenida todavía.

`SupportTicket = {id,subject,message,created_at,status:"Abierto"}`. Los contactos WhatsApp y la configuración pública se obtienen de variables `VITE_`; no requieren endpoint extra en la demo.

### Administración

| Método y ruta | Request | Response 200 | Errores adicionales |
|---|---|---|---|
| `GET /admin/stats` | Sin body | `{trial_users,premium_users,banned_users,total_users}` | 403 no admin |
| `GET /admin/users?query=&filter=all&page=0&limit=6` | `filter=all`, `trial`, `premium` o `banned`; página base 0 | `{users:User[],total,page,limit}` | 422 paginación/filtro |
| `POST /admin/users/:id/plan` | `{days}` y header `Idempotency-Key` de 8–128 caracteres | `User` actualizado más metadatos de activación | 404 usuario; 422 días/clave; 409 conflicto |
| `PATCH /admin/users/:id/ban` | `{until?:ISO,reason:string}` | `User` actualizado | 404 usuario; 409 admin; 422 motivo/duración |
| `PATCH /admin/users/:id/unban` | Sin body o `{reason}` | `User` actualizado | 404 usuario |

El contrato acepta activaciones de 1 a 3650 días; el backend las aplica en fragmentos idempotentes de hasta 365 días. Los baneos temporales son de 1 a 365 días. Omitir `until` significa permanente y exige confirmación explícita de la UI. El baneo no modifica `plan`, `plan_end` ni `trial_end`. El backend notifica al usuario bloqueado/desbloqueado y debe impedir todas las acciones administrativas si no es admin.

## Eventos En Tiempo Real

Transporte backend disponible: `POST /events/ticket` con Bearer válido entrega un ticket de 60 segundos de un solo uso; el cliente conecta a `WS /ws/events?ticket=...`. Los frames usan `{channel,event,payload,ts}` y `ts` es ISO UTC. El frontend conserva la misma API de consumidor `subscribeWs(listener) => unsubscribe()`; la integración del cliente con el transporte real queda para la fase de conexión de pantallas.

| Canal                 | Evento             | Payload                                                                                   |
| --------------------- | ------------------ | ----------------------------------------------------------------------------------------- | ----- | ------------- |
| `signals`             | `signal.new`       | Señal completa, con `event:{type:"new",result?,r?,via?}`                                  |
| `signals`             | `signal.touched`   | Señal completa, con `event:{type:"touched",result?,r?,via?}`                              |
| `signals`             | `signal.cancelled` | Señal completa, `cancel_reason` exacto y `event:{type:"cancelled",via?}`                  |
| `signals`             | `signal.tp1`       | Señal completa, `operation_result:"TP1_HIT"`, `event:{type:"tp1",result:"TP1",r,via?}`    |
| `signals`             | `signal.closed`    | Señal completa, `event:{type:"closed",result:"TP1"                                        | "TP2" | "SL",r,via?}` |
| `prices`              | `tick`             | `{symbol,price,change_pct,volume_24h,high_24h,low_24h}`; hasta 1/s por símbolo, solo los que cambian
| `prices`              | `funding.tick`     | `{symbol,funding_rate,mark_price,index_price,next_funding_time}`; procede del flujo mark-price de Binance                               |
| `market.liquidations` | `summary`          | `LiquidationSummary`, `window:5                                                           | 15    | 60`           |
| `market.movers`       | `snapshot`         | `MoversSnapshot`, `window:5                                                               | 15    | 60`           |
| `alerts`              | `alert.new`        | `AlertItem`                                                                               |
| `account`             | `plan.changed`     | `{user:User}` nuevo estado de cuenta                                                      |
| `account`             | `ban.changed`      | `{user_id?,banned,ban_until?,reason?}`; el canal del usuario no debe filtrar datos ajenos |
| `account`             | `payment.updated`  | `{record:PaymentRecord}`                                                                  |

No se anida la señal dentro de `{signal:...}`: el payload es la señal completa con su campo `event`. TP2/SL se comunican con `signal.closed` y `event.result`, sin inventar canales adicionales. Las notificaciones de cierre llevan a Resultados; las pendientes, tocadas y TP1 llevan a la señal. Los productores actuales publican cambios de señales, precios, liquidaciones, snapshots de movers, notificaciones, pagos y cambios directos de plan/baneo. Redis Pub/Sub no conserva eventos para usuarios desconectados; al iniciar o reconectar, el cliente debe resincronizar el estado mediante REST. Los cambios secundarios derivados de expiraciones/recompensas deben mantenerse cubiertos a medida que se cierren sus caminos de actualización.

## Variables Públicas

Ver `.env.example`. `VITE_API_URL` apunta a la raíz de FastAPI (por ejemplo `http://localhost:8000`); los endpoints de autenticación del backend actual no llevan prefijo `/v1`. `VITE_AUTH_MODE=api` activa las rutas reales de autenticación/cuenta/sesiones. `VITE_WS_URL` y `VITE_MOCK_STATE=error` siguen relacionadas con el flujo de datos analíticos de la maqueta. Nunca colocar secretos en variables `VITE_`.

Decisiones no especificadas previamente: la demo entra con usuario Premium/admin para facilitar la revisión; el catálogo contiene 17 símbolos; los diseños son preferencias locales persistentes; el snapshot de radar se transporta en lista plana; MMR de referencia 0,4%; riesgo porcentual en unidades completas; límite manual de activación 3650 días; persistencia de prueba en navegador. Ninguna de estas decisiones implica una conexión a servicios de producción.

## Análisis De Mercado — Backend Disponible En Fase 12

Estos endpoints ya están implementados en FastAPI, pero las páginas React continúan en modo maqueta hasta la fase posterior de integración. Activar `VITE_AUTH_MODE=api` no cambia automáticamente las funciones analíticas del cliente.

| Método y ruta | Respuesta | Notas |
|---|---|---|
| `GET /market/regime` | `MarketRegime` más `timeframes`, `confidence`, `score`, `reference`, `breadth` y `guidance` | Calcula con el módulo original `regime.py`, usando velas de Binance Futures 15m/1h/4h. Puede responder 503 si no hay suficiente histórico o falla la fuente principal. |
| `GET /derivatives?symbol=BTCUSDT` | `DerivativesData` más `raw`, `sources` y `updated_at` | Funding actual e histórico, interés abierto actual e histórico, cambios de OI solo si hay cobertura suficiente, ratios de cuentas y top traders (con `BINANCE_API_KEY` solo del lado backend), flujo taker por ventana, base del contrato y metadatos de frescura. Las métricas individuales se permiten `null` si su fuente no responde; no se sustituyen por ceros. |
| `GET /market/flow?symbol=BTCUSDT` | `FlowData` más `source`, `status`, `updated_at` y `raw` | Requiere velas reales de 5m; entrega ventanas 15m/1h/4h y puntos CVD con OHLCV muestreado. `time` se formatea según la zona horaria del usuario, `timestamp` son milisegundos Unix. |
| `GET /calendar?view=7` | `CalendarEventItem[]` | `view` admite 7 o 30 días. Combina FOMC, vencimientos Deribit, CPI/NFP de BLS cuando la fuente está disponible y el próximo funding de BTC si Binance lo devuelve. `X-FTrade-Calendar-BLS` permite observar disponibilidad/degradación de BLS. |

Todas las rutas están protegidas por el control de acceso analítico. Se reutiliza la caché compartida para reducir llamadas a Binance y BLS. La forma de respuesta permite campos adicionales del motor para evitar perder contexto; los tipos del frontend se actualizarán durante la conexión real de las pantallas, sin cambiar su diseño visual.


### Contrato real del backend — Fase 14

Las rutas `watchlist`, `account/alert-preferences`, `alerts/price` y `notifications` de arriba describen el backend actual. Su lógica y persistencia están implementadas, pero todavía no todas las pantallas React consumen estos endpoints; se conectarán después del cierre de la lógica de negocio, conservando la apariencia del frontend. La watchlist incluye `analyses` por temporalidad, `score_detail`, `liquidity_sweep` y `active_signal`; los campos analíticos que no se pueden calcular se devuelven como `null`. El worker `worker.user_alerts` refresca snapshots de mercado para que los avisos automáticos no dependan de una visita del usuario.
