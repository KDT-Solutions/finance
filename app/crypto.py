"""Encryption of secrets at rest (bexio refresh tokens)."""

from cryptography.fernet import Fernet, InvalidToken

from .config import get_settings

_fernet: Fernet | None = None


def _get() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(get_settings().data_encryption_key.encode())
    return _fernet


def encrypt(value: str | None) -> str | None:
    if not value:
        return None
    return _get().encrypt(value.encode()).decode()


def decrypt(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return _get().decrypt(value.encode()).decode()
    except InvalidToken:
        return None
