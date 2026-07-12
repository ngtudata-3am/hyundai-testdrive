"""SePay Payment Gateway helpers (checkout signature)."""

from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Any

# Thứ tự field khi ký — theo docs SePay (form-thanh-toan)
SIGNED_FIELDS = (
    "order_amount",
    "merchant",
    "currency",
    "operation",
    "order_description",
    "order_invoice_number",
    "customer_id",
    "payment_method",
    "success_url",
    "error_url",
    "cancel_url",
)

CHECKOUT_URLS = {
    "sandbox": "https://pay-sandbox.sepay.vn/v1/checkout/init",
    "production": "https://pay.sepay.vn/v1/checkout/init",
}


def sign_fields(fields: dict[str, Any], secret_key: str) -> str:
    parts = []
    for field in SIGNED_FIELDS:
        if field in fields and fields[field] not in (None, ""):
            parts.append(f"{field}={fields[field]}")
    message = ",".join(parts)
    digest = hmac.new(
        secret_key.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


def build_checkout_fields(
    *,
    merchant_id: str,
    secret_key: str,
    order_invoice_number: str,
    order_amount: int,
    order_description: str,
    customer_id: str,
    success_url: str,
    error_url: str,
    cancel_url: str,
    payment_method: str = "BANK_TRANSFER",
) -> dict[str, str]:
    fields: dict[str, str] = {
        "merchant": merchant_id,
        "currency": "VND",
        "order_amount": str(int(order_amount)),
        "operation": "PURCHASE",
        "payment_method": payment_method,
        "order_description": order_description,
        "order_invoice_number": order_invoice_number,
        "customer_id": customer_id,
        "success_url": success_url,
        "error_url": error_url,
        "cancel_url": cancel_url,
    }
    fields["signature"] = sign_fields(fields, secret_key)
    return fields


def checkout_url(env: str) -> str:
    return CHECKOUT_URLS.get(env, CHECKOUT_URLS["sandbox"])
