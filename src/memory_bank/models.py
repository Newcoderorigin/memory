from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    id: int
    namespace: str
    key: str
    value: str
    value_signature: str
    created_at: datetime
    updated_at: datetime
