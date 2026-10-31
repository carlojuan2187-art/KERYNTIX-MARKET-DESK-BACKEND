"""USDT BEP20 payment intents and confirmation, using read-only JSON-RPC."""
from __future__ import annotations

import os
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Protocol

import requests
from fastapi import HTTPException

from api.repositories.business import InMemoryBusinessRepository, MongoBusinessRepository
from api.services.plans import PlanService

UTC = timezone.utc
EVM_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
DEFAULT_USDT_CONTRACT = "0x55d398326f99059fF775485246999027B3197955"


def now_utc() -> datetime:
    return datetime.now(UTC)


def normalize_address(value: str) -> str:
    return str(value or "").strip().lower()


def is_valid_address(value: str) -> bool:
    return bool(EVM_RE.fullmatch(str(value or "").strip()))


def amount_to_units(value: Decimal, decimals: int = 6) -> int:
    scaled = value * (Decimal(10) ** decimals)
    integral = scaled.to_integral_value()
    if scaled != integral:
        raise ValueError("AMOUNT_PRECISION")
    return int(integral)


def amount_to_string(units: int, decimals: int = 6, display_decimals: int = 3) -> str:
    value = Decimal(int(units)) / (Decimal(10) ** decimals)
    return f"{value:.{display_decimals}f}"


class ChainVerifier(Protocol):
    def is_configured(self) -> bool: ...
    def find_matching_transfer(self, sender: str, payment: Mapping[str, Any]) -> dict[str, Any] | None: ...


class BscRpcVerifier:
    """Read-only BNB Smart Chain RPC verifier. Never holds or requests private keys."""
    def __init__(self, *, rpc_url: str | None = None, recipient: str | None = None,
                 token_contract: str | None = None, confirmations: int | None = None,
                 chunk_blocks: int | None = None, max_chunks: int | None = None,
                 timeout_seconds: float = 15.0) -> None:
        self.rpc_url = (rpc_url if rpc_url is not None else os.getenv("BSC_RPC_URL", "https://bsc-dataseed.binance.org")).strip()
        self.recipient = normalize_address(recipient if recipient is not None else os.getenv("PAYMENT_WALLET_ADDRESS", ""))
        self.token_contract = normalize_address(token_contract if token_contract is not None else os.getenv("USDT_BEP20_CONTRACT", DEFAULT_USDT_CONTRACT))
        self.confirmations = max(1, int(confirmations if confirmations is not None else os.getenv("BSC_MIN_CONFIRMATIONS", "12")))
        self.chunk_blocks = max(100, int(chunk_blocks if chunk_blocks is not None else os.getenv("BSC_LOG_CHUNK_BLOCKS", "4000")))
        self.max_chunks = max(1, int(max_chunks if max_chunks is not None else os.getenv("BSC_MAX_CHUNKS", "40")))
        self.timeout_seconds = timeout_seconds
        self.chain_decimals = max(0, min(36, int(os.getenv("USDT_CHAIN_DECIMALS", "18"))))
        self.payment_decimals = 6

    def is_configured(self) -> bool:
        placeholders = ("replace-with", "placeholder", "changeme")
        return is_valid_address(self.recipient) and is_valid_address(self.token_contract) and bool(self.rpc_url) and not any(p in self.recipient for p in placeholders)

    def _rpc(self, method: str, params: list[Any]) -> Any:
        response = requests.post(self.rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=self.timeout_seconds)
        response.raise_for_status()
        body = response.json()
        if body.get("error"):
            raise RuntimeError(f"BSC_RPC_{method}_ERROR")
        return body.get("result")

    @staticmethod
    def _addr_topic(address: str) -> str:
        return "0x" + "0" * 24 + normalize_address(address)[2:]

    def _block_timestamp(self, block_number: int) -> int:
        block = self._rpc("eth_getBlockByNumber", [hex(block_number), False])
        if not isinstance(block, dict) or not block.get("timestamp"):
            raise RuntimeError("BSC_BLOCK_UNAVAILABLE")
        return int(block["timestamp"], 16)

    def find_matching_transfer(self, sender: str, payment: Mapping[str, Any]) -> dict[str, Any] | None:
        if not self.is_configured():
            raise RuntimeError("PAYMENTS_NOT_CONFIGURED")
        sender = normalize_address(sender)
        created = payment["created_at"]
        expires = payment["expires_at"]
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        created_ts, expires_ts = int(created.timestamp()), int(expires.timestamp())
        latest = int(self._rpc("eth_blockNumber", []), 16)
        safe_head = latest - self.confirmations + 1
        if safe_head < 0:
            return None
        target_units = int(payment["expected_units"])
        if self.chain_decimals < self.payment_decimals:
            raise RuntimeError("USDT_CHAIN_DECIMALS_INVALID")
        expected_chain_units = target_units * (10 ** (self.chain_decimals - self.payment_decimals))
        to_block = safe_head
        candidates: list[dict[str, Any]] = []
        block_times: dict[int, int] = {}
        for _ in range(self.max_chunks):
            if to_block < 0:
                break
            from_block = max(0, to_block - self.chunk_blocks + 1)
            logs = self._rpc("eth_getLogs", [{
                "fromBlock": hex(from_block), "toBlock": hex(to_block),
                "address": self.token_contract,
                "topics": [TRANSFER_TOPIC, self._addr_topic(sender), self._addr_topic(self.recipient)],
            }]) or []
            for entry in logs:
                try:
                    block_number = int(entry["blockNumber"], 16)
                    log_index = int(entry["logIndex"], 16)
                    value = int(entry["data"], 16)
                    txhash = str(entry["transactionHash"]).lower()
                    if not re.fullmatch(r"0x[a-f0-9]{64}", txhash):
                        continue
                    if block_number not in block_times:
                        block_times[block_number] = self._block_timestamp(block_number)
                    timestamp = block_times[block_number]
                    if value == expected_chain_units and created_ts <= timestamp <= expires_ts:
                        candidates.append({"txid": f"{txhash}:{log_index}", "tx_hash": txhash, "log_index": log_index,
                                           "block_number": block_number, "timestamp": timestamp, "value_units": value})
                except (KeyError, TypeError, ValueError):
                    continue
            if from_block not in block_times:
                block_times[from_block] = self._block_timestamp(from_block)
            oldest_ts = block_times[from_block]
            if oldest_ts < created_ts:
                break
            to_block = from_block - 1
        candidates.sort(key=lambda x: (x["block_number"], x["log_index"]))
        return candidates[0] if candidates else None


class PaymentService:
    PRICE_USDT = Decimal("10.00")
    PLAN_DAYS = 30

    def __init__(self, repository: Any, plan_service: PlanService, chain_verifier: ChainVerifier | None = None,
                 *, price_usdt: Decimal | None = None, plan_days: int = 30, referral_service: Any | None = None,
                 business_notifications: Any | None = None) -> None:
        self.repository = repository
        self.plan_service = plan_service
        self.chain = chain_verifier or BscRpcVerifier()
        self.referral_service = referral_service
        self.business_notifications = business_notifications
        raw_price = price_usdt if price_usdt is not None else Decimal(os.getenv("PREMIUM_PRICE_USDT", "10.00"))
        if not raw_price.is_finite() or raw_price <= 0:
            raise ValueError("PREMIUM_PRICE_USDT debe ser un decimal positivo")
        if raw_price.quantize(Decimal("0.001")) != raw_price:
            raise ValueError("PREMIUM_PRICE_USDT debe tener como máximo tres decimales para que el importe mostrado coincida con el verificado")
        self.price_units = amount_to_units(raw_price, 6)
        self.plan_days = max(1, min(365, int(plan_days)))
        self.window_minutes = max(10, min(1440, int(os.getenv("PAYMENT_WINDOW_MINUTES", "180"))))
        self.max_attempts = max(1, min(100, int(os.getenv("PAYMENT_MAX_ATTEMPTS", "20"))))
        self.cooldown_seconds = max(0, min(300, int(os.getenv("PAYMENT_RETRY_COOLDOWN_SECONDS", "8"))))
        self.verify_lease_seconds = max(30, min(600, int(os.getenv("PAYMENT_VERIFY_LEASE_SECONDS", "90"))))

    @staticmethod
    def _public(doc: Mapping[str, Any]) -> dict[str, Any]:
        units = int(doc["expected_units"])
        return {"payment_id": str(doc["payment_id"]), "status": str(doc["status"]),
                "network": "BNB Smart Chain (BEP20)", "token": "USDT", "wallet": doc.get("wallet"),
                "amount_usdt": amount_to_string(units, 6, 3), "amount_units": units,
                "created_at": _iso(doc.get("created_at")), "expires_at": _iso(doc.get("expires_at")),
                "completed_at": _iso(doc.get("completed_at")), "tx_hash": doc.get("tx_hash"),
                "txid": doc.get("txid"), "activation_status": doc.get("activation_status", "not_started"),
                "attempts": int(doc.get("attempts", 0))}

    def _wallet(self) -> str:
        value = normalize_address(os.getenv("PAYMENT_WALLET_ADDRESS", ""))
        if not is_valid_address(value):
            raise HTTPException(status_code=503, detail={"code": "PAYMENTS_NOT_CONFIGURED", "message": "Los pagos USDT todavía no están configurados en el servidor."})
        return value

    def _enqueue_payment_notification(self, payment: Mapping[str, Any]) -> None:
        service = self.business_notifications
        if service is None:
            return
        payment_id = str(payment.get("payment_id") or "")
        user_id = str(payment.get("user_id") or "")
        status_value = str(payment.get("status") or "").lower()
        if not payment_id or not user_id:
            return
        if status_value == "completed":
            event_id = f"payment-completed:{payment_id}"
            title = "Pago confirmado · Premium activado"
            message = "Tu transferencia USDT ha sido confirmada y tu suscripción Premium está activa."
            business_event = "payment.completed"
        elif status_value == "verified":
            event_id = f"payment-verified:{payment_id}"
            title = "Transferencia confirmada"
            message = "La transferencia fue verificada. Estamos terminando la activación de Premium; no necesitas volver a enviar el pago."
            business_event = "payment.verified"
        elif status_value == "pending":
            event_id = f"payment-created:{payment_id}"
            title = "Solicitud de pago creada"
            amount_text = payment.get("amount_usdt") or amount_to_string(int(payment.get("expected_units") or 0), 6, 3)
            message = f"Tu solicitud de pago de {amount_text} USDT está lista. Completa la transferencia antes de su vencimiento."
            business_event = "payment.created"
        else:
            return
        try:
            service.enqueue(user_id, event_id=event_id, title=title, message=message,
                            business_event=business_event,
                            metadata={"payment_id": payment_id, "payment_status": status_value},
                            now=payment.get("updated_at") if isinstance(payment.get("updated_at"), datetime) else None)
        except Exception:
            # Payment state is canonical and already persisted. Notification failure
            # is logged and retried by later idempotent reads/reconciliation.
            import logging
            logging.getLogger("ftrade.payments").exception("No se pudo encolar notificación de pago")

    def create_or_get(self, user_id: str, *, now: datetime | None = None) -> dict[str, Any]:
        current = now or now_utc()
        if not self.chain.is_configured():
            raise HTTPException(status_code=503, detail={"code": "PAYMENTS_NOT_CONFIGURED", "message": "La verificación de pagos todavía no está configurada."})
        self.repository.expire_payments(current)
        existing = self.repository.get_active_payment(str(user_id), current)
        if existing:
            if existing.get("status") == "verified":
                existing = self._try_finish_verified(existing, current)
            self._enqueue_payment_notification(existing)
            return self._public(existing)
        active_amounts = self.repository.active_amounts(current)
        # Match the current bot's unique amount scheme: plan price + 0.001..0.099 USDT.
        candidates = [self.price_units + offset * 1000 for offset in range(1, 100) if self.price_units + offset * 1000 not in active_amounts]
        secrets.SystemRandom().shuffle(candidates)
        wallet = self._wallet()
        for amount_units in candidates:
            payment_id = uuid.uuid4().hex
            doc = {"payment_id": payment_id, "user_id": str(user_id), "status": "pending", "wallet": wallet,
                   "network": "BEP20", "token": "USDT", "expected_units": amount_units,
                   "created_at": current, "expires_at": current + timedelta(minutes=self.window_minutes),
                   "attempts": 0, "last_attempt_at": None, "activation_status": "not_started",
                   "active_slot": str(user_id), "active_amount": amount_units, "created_by": "self_service"}
            created = self.repository.create_payment(doc)
            if created:
                self._enqueue_payment_notification(created)
                return self._public(created)
        raise HTTPException(status_code=503, detail={"code": "PAYMENT_AMOUNT_CAPACITY", "message": "No hay importes de pago disponibles en este momento."})

    def current(self, user_id: str, *, now: datetime | None = None) -> dict[str, Any] | None:
        current = now or now_utc()
        self.repository.expire_payments(current)
        doc = self.repository.get_active_payment(user_id, current)
        if not doc:
            return None
        if doc.get("status") == "verified":
            doc = self._try_finish_verified(doc, current)
        self._enqueue_payment_notification(doc)
        return self._public(doc)

    def history(self, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        return [self._public(doc) for doc in self.repository.list_payments(user_id, limit)]

    @staticmethod
    def _admin_public(doc: Mapping[str, Any]) -> dict[str, Any]:
        """Sanitized payment view for administrators; no RPC payloads or internal leases."""
        units = int(doc.get("expected_units", 0))
        return {
            "payment_id": str(doc.get("payment_id", "")), "user_id": str(doc.get("user_id", "")),
            "status": str(doc.get("status", "unknown")), "network": "BNB Smart Chain (BEP20)",
            "token": "USDT", "wallet": doc.get("wallet"), "sender_address": doc.get("sender_address"),
            "amount_usdt": amount_to_string(units, 6, 3), "amount_units": units,
            "created_at": _iso(doc.get("created_at")), "expires_at": _iso(doc.get("expires_at")),
            "verified_at": _iso(doc.get("verified_at")), "completed_at": _iso(doc.get("completed_at")),
            "tx_hash": doc.get("tx_hash"), "txid": doc.get("txid"),
            "activation_status": doc.get("activation_status", "not_started"),
            "activation_error": doc.get("activation_error"), "last_error_code": doc.get("last_error_code"),
            "attempts": int(doc.get("attempts", 0)),
        }

    def admin_history(self, *, status: str | None = None, limit: int = 100) -> dict[str, Any]:
        docs = self.repository.list_all_payments(status=status, limit=max(1, min(int(limit), 200)))
        return {"items": [self._admin_public(doc) for doc in docs], "count": len(docs), "status": status}

    def retry_activation(self, payment_id: str, actor_id: str, *, now: datetime | None = None) -> dict[str, Any]:
        current = now or now_utc()
        payment = self.repository.get_payment(str(payment_id))
        if not payment:
            raise HTTPException(status_code=404, detail={"code": "PAYMENT_NOT_FOUND", "message": "No se encontró ese pago."})
        if payment.get("status") == "completed":
            return {"result": "ok", "payment": self._admin_public(payment)}
        if payment.get("status") != "verified":
            raise HTTPException(status_code=409, detail={"code": "PAYMENT_NOT_VERIFIED", "message": "Solo se puede reintentar la activación de un pago cuya transferencia ya fue verificada."})
        result = self._try_finish_verified(payment, current)
        self.repository.write_audit({
            "event_id": uuid.uuid4().hex, "actor_id": str(actor_id), "action": "payment_activation_retry",
            "target_user_id": str(result.get("user_id", "")),
            "details": {"payment_id": str(payment_id), "status": result.get("status"),
                        "activation_status": result.get("activation_status")}, "created_at": current,
        })
        return {"result": "ok" if result.get("status") == "completed" else "activation_pending",
                "payment": self._admin_public(result)}

    def reconcile_verified_activation(self, payment_id: str, *, now: datetime | None = None) -> dict[str, Any]:
        """Retry Premium activation only for a transfer already durably verified.

        This is called by the background reconciler. It never queries the chain and
        therefore cannot turn an unverified payment into a subscription.
        """
        current = now or now_utc()
        payment = self.repository.get_payment(str(payment_id))
        if not payment:
            return {"payment_id": str(payment_id), "result": "not_found"}
        if str(payment.get("status")) == "completed":
            return {"payment_id": str(payment_id), "result": "already_completed"}
        if str(payment.get("status")) != "verified":
            return {"payment_id": str(payment_id), "result": "not_verified"}
        updated = self._try_finish_verified(payment, current)
        return {
            "payment_id": str(payment_id),
            "result": "completed" if str(updated.get("status")) == "completed" else "activation_pending",
            "activation_status": updated.get("activation_status", "pending"),
        }

    def _try_finish_verified(self, payment: Mapping[str, Any], now: datetime) -> dict[str, Any]:
        payment_id = str(payment["payment_id"])
        try:
            return self._finish_verified(payment, now)
        except Exception as exc:
            # A confirmed transfer remains durably verified. Future GET/verify/admin retry
            # requests can resume the idempotent plan activation without rescanning chain.
            self.repository.update_payment(payment_id, {
                "status": "verified", "activation_status": "pending",
                "activation_error": type(exc).__name__, "updated_at": now_utc(),
            }, unset=("verification_lease_until",))
            recovered = self.repository.get_payment(payment_id) or dict(payment)
            self._enqueue_payment_notification(recovered)
            return recovered

    def verify(self, user_id: str, payment_id: str, sender_address: str, *, now: datetime | None = None) -> dict[str, Any]:
        current = now or now_utc()
        sender = normalize_address(sender_address)
        wallet = self._wallet()
        if not is_valid_address(sender) or sender == wallet:
            raise HTTPException(status_code=422, detail={"code": "INVALID_SENDER_ADDRESS", "message": "Introduce una dirección pública válida de la wallet que envió los USDT."})
        payment = self.repository.get_payment(payment_id, user_id)
        if not payment:
            raise HTTPException(status_code=404, detail={"code": "PAYMENT_NOT_FOUND", "message": "No se encontró ese pago para esta cuenta."})
        if payment.get("status") == "verified":
            result = self._try_finish_verified(payment, current)
            return {"result": "activation_pending" if result.get("status") != "completed" else "ok", "payment": self._public(result)}
        if payment.get("status") == "completed":
            return {"result": "ok", "payment": self._public(payment)}
        if payment.get("status") not in {"pending", "verifying"}:
            raise HTTPException(status_code=409, detail={"code": "PAYMENT_NOT_OPEN", "message": "Este pago ya no está pendiente."})
        if payment.get("expires_at") <= current:
            self.repository.update_payment(payment_id, {"status": "expired", "updated_at": current}, unset=("active_slot", "active_amount", "verification_lease_until"))
            raise HTTPException(status_code=410, detail={"code": "PAYMENT_EXPIRED", "message": "La solicitud de pago venció. Crea una nueva solicitud."})
        if int(payment.get("attempts", 0)) >= self.max_attempts:
            raise HTTPException(status_code=429, detail={"code": "PAYMENT_ATTEMPTS_EXCEEDED", "message": "Se alcanzó el límite de verificaciones para este pago."})
        last = payment.get("last_attempt_at")
        if last and (current - _aware(last)).total_seconds() < self.cooldown_seconds:
            raise HTTPException(status_code=429, detail={"code": "PAYMENT_COOLDOWN", "message": "Espera unos segundos antes de verificar nuevamente."})
        claimed = self.repository.claim_payment_verification(payment_id, user_id, now=current,
                    lease_until=current + timedelta(seconds=self.verify_lease_seconds), sender=sender)
        if not claimed:
            raise HTTPException(status_code=409, detail={"code": "PAYMENT_VERIFICATION_IN_PROGRESS", "message": "Este pago ya se está verificando. Intenta de nuevo en unos segundos."})
        try:
            transfer = self.chain.find_matching_transfer(sender, claimed)
        except Exception as exc:
            self.repository.update_payment(payment_id, {"status": "pending", "last_error_code": "CHAIN_UNAVAILABLE", "updated_at": now_utc()}, unset=("verification_lease_until",))
            raise HTTPException(status_code=503, detail={"code": "CHAIN_UNAVAILABLE", "message": "No se pudo consultar la red BNB Smart Chain. El pago sigue pendiente y puedes intentarlo otra vez."}) from exc
        if not transfer:
            self.repository.update_payment(payment_id, {"status": "pending", "last_error_code": "TRANSFER_NOT_FOUND", "updated_at": now_utc()}, unset=("verification_lease_until",))
            return {"result": "not_found", "payment": self._public(self.repository.get_payment(payment_id, user_id) or claimed)}
        # Validate the verifier's response again at the business boundary, so an
        # adapter error cannot credit the wrong amount or an out-of-window transfer.
        try:
            chain_decimals = max(6, min(36, int(os.getenv("USDT_CHAIN_DECIMALS", "18"))))
            exact_value = int(claimed["expected_units"]) * (10 ** (chain_decimals - 6))
            timestamp = int(transfer["timestamp"])
            tx_hash = str(transfer["tx_hash"]).lower()
            log_index = int(transfer["log_index"])
            block_number = int(transfer["block_number"])
            expected_txid = f"{tx_hash}:{log_index}"
            created_ts = int(_aware(claimed["created_at"]).timestamp())
            expires_ts = int(_aware(claimed["expires_at"]).timestamp())
            response_valid = (int(transfer["value_units"]) == exact_value and created_ts <= timestamp <= expires_ts
                              and re.fullmatch(r"0x[a-f0-9]{64}", tx_hash) is not None and log_index >= 0 and block_number >= 0
                              and str(transfer["txid"]).lower() == expected_txid)
        except (KeyError, TypeError, ValueError, OverflowError):
            response_valid = False
        if not response_valid:
            self.repository.update_payment(payment_id, {"status": "pending", "last_error_code": "INVALID_CHAIN_RESPONSE", "updated_at": now_utc()}, unset=("verification_lease_until",))
            raise HTTPException(status_code=503, detail={"code": "INVALID_CHAIN_RESPONSE", "message": "La respuesta de verificación de la cadena no pasó la validación de seguridad."})
        txid = str(transfer["txid"]).lower()
        tx_claimed = self.repository.claim_payment_tx(txid, {"user_id": str(user_id), "payment_id": payment_id,
                            "tx_hash": transfer.get("tx_hash"), "log_index": transfer.get("log_index"),
                            "block_number": transfer.get("block_number"), "amount_units": int(claimed["expected_units"]), "used_at": current})
        if not tx_claimed:
            self.repository.update_payment(payment_id, {"status": "pending", "last_error_code": "TX_ALREADY_USED", "updated_at": current}, unset=("verification_lease_until",))
            raise HTTPException(status_code=409, detail={"code": "TX_ALREADY_USED", "message": "Esta transferencia ya fue asociada a otro pago."})
        verified = self.repository.update_payment(payment_id, {"status": "verified", "txid": txid, "tx_hash": transfer.get("tx_hash"),
                   "log_index": transfer.get("log_index"), "block_number": transfer.get("block_number"), "chain_timestamp": transfer.get("timestamp"),
                   "verified_at": current, "activation_status": "pending", "updated_at": current}, unset=("verification_lease_until",))
        try:
            verified = self._finish_verified(verified or claimed, current)
        except Exception as exc:
            # Transaction claim + verified record remain durable; retry calls can
            # continue plan activation without scanning chain or claiming tx twice.
            self.repository.update_payment(payment_id, {"activation_status": "pending", "activation_error": type(exc).__name__, "updated_at": now_utc()})
            pending = self.repository.get_payment(payment_id, user_id) or verified or claimed
            self._enqueue_payment_notification(pending)
            return {"result": "activation_pending", "payment": self._public(pending)}
        return {"result": "ok", "payment": self._public(verified)}

    def _finish_verified(self, payment: Mapping[str, Any], now: datetime) -> dict[str, Any]:
        payment_id = str(payment["payment_id"])
        user_id = str(payment["user_id"])
        txid = str(payment.get("txid") or "")
        if not txid:
            raise RuntimeError("VERIFIED_PAYMENT_MISSING_TXID")
        activation = self.plan_service.activate_premium(user_id, days=self.plan_days, source="payment", actor_id=None,
                    reference=txid, idempotency_key=f"payment:{txid}", now=now)
        updated = self.repository.update_payment(payment_id, {"status": "completed", "activation_status": "completed",
                    "completed_at": now, "updated_at": now, "activation_event_id": activation.get("event_id")},
                    unset=("active_slot", "active_amount", "activation_error", "verification_lease_until"))
        if updated is None:
            raise RuntimeError("PAYMENT_RECORD_LOST_AFTER_ACTIVATION")
        self._enqueue_payment_notification(updated)
        # If retrying after a crash, the central plan idempotency key makes this safe.
        try:
            self._after_first_premium_activation(user_id, f"payment:{txid}")
        except Exception:
            # Payment/plan completion remains durable; referral rewards are separately retryable.
            pass
        return updated

    def _after_first_premium_activation(self, user_id: str, activation_event: str) -> None:
        if self.referral_service is not None:
            self.referral_service.on_premium_activated(user_id, event_id=activation_event)


def _aware(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    raise ValueError("Expected datetime")


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = _aware(value)
        return dt.isoformat().replace("+00:00", "Z")
    return str(value)
