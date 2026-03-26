from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class DataBankConfig:
    """Runtime configuration for the memory data bank."""

    db_path: Path
    api_key_hash: str
    signing_key: str

    @classmethod
    def from_env(
        cls,
        *,
        db_path: str = "./data/memory_bank.sqlite3",
        api_key_hash: str = "",
        signing_key: str = "",
    ) -> "DataBankConfig":
        resolved_path = Path(db_path).expanduser().resolve()
        resolved_path.parent.mkdir(parents=True, exist_ok=True)

        if len(api_key_hash) < 32:
            raise ValueError("api_key_hash must be a secure hash with length >= 32")
        if len(signing_key) < 32:
            raise ValueError("signing_key must contain at least 32 characters")

        return cls(
            db_path=resolved_path,
            api_key_hash=api_key_hash,
            signing_key=signing_key,
        )
