from __future__ import annotations

import base64
import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from nl2sql_service.core.config import settings


class KeyVaultUnavailableError(RuntimeError):
    pass


def _resolve_secret(secret: str | None = None) -> bytes:
    configured = secret if secret is not None else settings.provider_key_encryption_secret
    if configured is None:
        raise KeyVaultUnavailableError("Provider key encryption is not configured.")
    cleaned = configured.strip()
    if not cleaned:
        raise KeyVaultUnavailableError("Provider key encryption is not configured.")
    return hashlib.sha256(cleaned.encode("utf-8")).digest()


def is_key_vault_configured() -> bool:
    try:
        _resolve_secret()
    except KeyVaultUnavailableError:
        return False
    return True


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:16]


def encrypt_api_key(raw_key: str, *, secret: str | None = None) -> tuple[str, str]:
    key = _resolve_secret(secret)
    iv = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(iv, raw_key.encode("utf-8"), None)
    encoded = base64.b64encode(iv + ciphertext).decode("ascii")
    return hash_api_key(raw_key), encoded


def decrypt_api_key(encoded: str, *, secret: str | None = None) -> str:
    key = _resolve_secret(secret)
    payload = base64.b64decode(encoded.encode("ascii"))
    if len(payload) < 13:
        raise ValueError("Encrypted provider key payload is invalid.")
    iv, ciphertext = payload[:12], payload[12:]
    raw = AESGCM(key).decrypt(iv, ciphertext, None)
    return raw.decode("utf-8")
