"""Encrypt per-user API keys at rest.

Scheme: derive a 32-byte data key from the server secret (OWQ_SECRET) via
HKDF-SHA256, then AES-256-GCM with a random 12-byte nonce. The GCM additional
authenticated data (AAD) binds each ciphertext to its owning user_id + key
version, so a row copied to another user fails to decrypt (defends against
row swapping). The plaintext key is decrypted only at call time and never
logged, exported, or shown to the client (only a masked hint is stored).

Requires the `cryptography` package (declared in pyproject extras).
"""
from __future__ import annotations

import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

KEY_VERSION = 1
_HKDF_SALT = b"owq-ai-key-v1"
_HKDF_INFO = b"owq-ai-user-api-key"
_NONCE_BYTES = 12


def _data_key(secret: str) -> bytes:
    if not secret:
        raise ValueError("server secret is empty; cannot derive AI key-encryption key")
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_HKDF_SALT,
        info=_HKDF_INFO,
    ).derive(secret.encode("utf-8"))


def _aad(user_id: int, key_version: int) -> bytes:
    return f"ai-key:{int(user_id)}:{int(key_version)}".encode("utf-8")


def encrypt_api_key(
    secret: str, user_id: int, plaintext: str, key_version: int = KEY_VERSION
) -> tuple[bytes, bytes]:
    """Return (ciphertext, nonce). plaintext is the user's raw API key."""
    if not plaintext:
        raise ValueError("API key is empty")
    nonce = os.urandom(_NONCE_BYTES)
    ciphertext = AESGCM(_data_key(secret)).encrypt(
        nonce, plaintext.encode("utf-8"), _aad(user_id, key_version)
    )
    return ciphertext, nonce


def decrypt_api_key(
    secret: str,
    user_id: int,
    ciphertext: bytes,
    nonce: bytes,
    key_version: int = KEY_VERSION,
) -> str:
    """Inverse of encrypt_api_key. Raises cryptography.InvalidTag on tamper / wrong user."""
    plaintext = AESGCM(_data_key(secret)).decrypt(
        bytes(nonce), bytes(ciphertext), _aad(user_id, key_version)
    )
    return plaintext.decode("utf-8")


def mask_key(plaintext: str) -> str:
    """A safe-to-display hint, e.g. 'sk-…a90c'. Never reveals the full key."""
    raw = (plaintext or "").strip()
    if len(raw) <= 8:
        return "****"
    prefix = raw[:3]
    return f"{prefix}…{raw[-4:]}"
