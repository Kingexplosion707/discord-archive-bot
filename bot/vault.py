"""Encryption at rest for the Google login."""
from __future__ import annotations

from cryptography.fernet import Fernet


class Vault:
    def __init__(self, key: str) -> None:
        self._fernet = Fernet(key.encode("ascii"))

    def seal(self, data: bytes) -> bytes:
        return self._fernet.encrypt(data)

    def unseal(self, blob: bytes) -> bytes:
        return self._fernet.decrypt(blob)
