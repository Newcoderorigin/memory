from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from memory_bank.config import DataBankConfig
from memory_bank.security import derive_key, hash_api_key
from memory_bank.service import AuthenticationError, MemoryDataBankService


class MemoryDataBankTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.api_key = "super-secure-api-key"
        self.config = DataBankConfig.from_env(
            db_path=str(Path(self.tempdir.name) / "memory.sqlite3"),
            api_key_hash=hash_api_key(self.api_key),
            signing_key=derive_key("a-signing-secret-that-is-strong"),
        )
        self.service = MemoryDataBankService(self.config)
        self.service.initialize()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_store_and_retrieve(self) -> None:
        self.service.store(
            api_key=self.api_key,
            namespace="agent",
            key="favorite_color",
            value="blue",
        )

        record = self.service.retrieve(
            api_key=self.api_key,
            namespace="agent",
            key="favorite_color",
        )
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.value, "blue")

    def test_invalid_key_rejected(self) -> None:
        with self.assertRaises(AuthenticationError):
            self.service.store(
                api_key="wrong-key-xxxxxxxx",
                namespace="agent",
                key="favorite_color",
                value="blue",
            )

    def test_namespace_listing(self) -> None:
        self.service.store(api_key=self.api_key, namespace="ops", key="region", value="us-east")
        self.service.store(api_key=self.api_key, namespace="ops", key="tier", value="prod")

        records = self.service.list_namespace(api_key=self.api_key, namespace="ops")
        self.assertEqual([r.key for r in records], ["region", "tier"])


if __name__ == "__main__":
    unittest.main()
