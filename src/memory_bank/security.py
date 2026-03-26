from __future__ import annotations

import hashlib
import hmac
import os
from base64 import urlsafe_b64encode


def derive_key(raw_secret: str) -> str:
    """Derive a stable 256-bit key from an operator supplied secret."""
    if len(raw_secret) < 16:
        raise ValueError("raw_secret must be at least 16 characters")
    digest = hashlib.sha256(raw_secret.encode("utf-8")).digest()
    return urlsafe_b64encode(digest).decode("utf-8")


def hash_api_key(api_key: str, *, salt: bytes | None = None) -> str:
    """Hash API key using scrypt for slow brute-force resistance."""
    if not api_key or len(api_key) < 16:
        raise ValueError("api_key must be at least 16 characters")

    active_salt = salt if salt is not None else os.urandom(16)
    digest = hashlib.scrypt(
        api_key.encode("utf-8"),
        salt=active_salt,
        n=2**14,
        r=8,
        p=1,
        dklen=32,
    )
    return f"{active_salt.hex()}:{digest.hex()}"


def verify_api_key(api_key: str, encoded_hash: str) -> bool:
    """Verify api key using constant-time comparison."""
    try:
        salt_hex, digest_hex = encoded_hash.split(":", maxsplit=1)
    except ValueError:
        return False

    salt = bytes.fromhex(salt_hex)
    candidate = hashlib.scrypt(
        api_key.encode("utf-8"),
        salt=salt,
        n=2**14,
        r=8,
        p=1,
        dklen=32,
    ).hex()
    return hmac.compare_digest(candidate, digest_hex)


def sign_payload(payload: str, signing_key: str) -> str:
    digest = hmac.new(
        signing_key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return digest


def verify_signature(payload: str, signature: str, signing_key: str) -> bool:
    expected = sign_payload(payload, signing_key)
    return hmac.compare_digest(expected, signature)
