from __future__ import annotations

from .config import DataBankConfig
from .models import MemoryRecord
from .repository import MemoryRepository
from .security import sign_payload, verify_api_key, verify_signature


class AuthenticationError(PermissionError):
    """Raised when an API key check fails."""


class IntegrityError(ValueError):
    """Raised when payload integrity verification fails."""


class MemoryDataBankService:
    def __init__(self, config: DataBankConfig) -> None:
        self._config = config
        self._repo = MemoryRepository(config.db_path)

    def initialize(self) -> None:
        self._repo.initialize()

    def store(self, *, api_key: str, namespace: str, key: str, value: str) -> None:
        self._authenticate(api_key)
        self._validate(namespace, key, value)

        signature = sign_payload(value, self._config.signing_key)
        self._repo.upsert(namespace, key, value, signature)

    def retrieve(self, *, api_key: str, namespace: str, key: str) -> MemoryRecord | None:
        self._authenticate(api_key)
        record = self._repo.get(namespace, key)
        if record is None:
            return None
        if not verify_signature(record.value, record.value_signature, self._config.signing_key):
            raise IntegrityError("stored payload failed signature verification")
        return record

    def list_namespace(self, *, api_key: str, namespace: str) -> list[MemoryRecord]:
        self._authenticate(api_key)
        records = self._repo.list_namespace(namespace)
        for item in records:
            if not verify_signature(item.value, item.value_signature, self._config.signing_key):
                raise IntegrityError(f"record with key '{item.key}' failed signature verification")
        return records

    def _authenticate(self, api_key: str) -> None:
        if not verify_api_key(api_key, self._config.api_key_hash):
            raise AuthenticationError("invalid api key")

    @staticmethod
    def _validate(namespace: str, key: str, value: str) -> None:
        if not namespace.strip():
            raise ValueError("namespace is required")
        if not key.strip():
            raise ValueError("key is required")
        if len(key) > 128:
            raise ValueError("key must be <= 128 chars")
        if len(value) > 65535:
            raise ValueError("value must be <= 65535 chars")
