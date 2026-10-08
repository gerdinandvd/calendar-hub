"""AES-GCM storage for source secrets. The key stays in the environment."""

import base64
import hashlib
import hmac
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE_BYTES = 12


class SecretKeyError(Exception):
    """HUB_SECRET_KEY is missing or not a 32-byte url-safe base64 key."""


def encrypt(plaintext: str) -> str:
    aes = AESGCM(_key())
    nonce = os.urandom(_NONCE_BYTES)
    cipher = aes.encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.b64encode(nonce + cipher).decode("ascii")


def decrypt(stored: str) -> str:
    try:
        blob = base64.b64decode(stored, validate=True)
        nonce, cipher = blob[:_NONCE_BYTES], blob[_NONCE_BYTES:]
        if len(nonce) != _NONCE_BYTES or not cipher:
            raise ValueError
        plain = AESGCM(_key()).decrypt(nonce, cipher, None)
    except SecretKeyError:
        raise
    except Exception as exc:
        raise SecretKeyError("The secret key is not configured.") from exc
    return plain.decode("utf-8")


def equal(left: str, right: str) -> bool:
    digest = hashlib.sha256
    return hmac.compare_digest(digest(left.encode("utf-8")).digest(), digest(right.encode("utf-8")).digest())


def _key() -> bytes:
    raw = os.environ.get("HUB_SECRET_KEY", "").strip()
    if not raw:
        raise SecretKeyError("The secret key is not configured.")
    try:
        key = base64.urlsafe_b64decode(raw)
    except Exception as exc:
        raise SecretKeyError("The secret key is not configured.") from exc
    if len(key) != 32:
        raise SecretKeyError("The secret key is not configured.")
    return key
