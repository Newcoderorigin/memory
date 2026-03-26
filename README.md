# Memory Data Bank

Secure local memory data bank implemented for Python 3.13 with:

- API key authentication (scrypt-hashed key verification)
- HMAC payload signatures for integrity verification
- SQLite persistence with WAL and strict parameterized queries
- Layered architecture (config/security/repository/service/cli)

## Quickstart

```bash
python3.13 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Generate configuration values:

```bash
memory-bank bootstrap --api-key "your-strong-api-key" --signing-secret "your-long-signing-secret"
```

Initialize:

```bash
memory-bank init \
  --db-path ./data/memory_bank.sqlite3 \
  --api-key-hash '<api_key_hash>' \
  --signing-key '<signing_key>' \
  --api-key 'your-strong-api-key'
```

Store and retrieve:

```bash
memory-bank put --db-path ./data/memory_bank.sqlite3 --api-key-hash '<api_key_hash>' --signing-key '<signing_key>' --api-key 'your-strong-api-key' --namespace app --key greeting --value "hello"
memory-bank get --db-path ./data/memory_bank.sqlite3 --api-key-hash '<api_key_hash>' --signing-key '<signing_key>' --api-key 'your-strong-api-key' --namespace app --key greeting
```

## Testing

```bash
python -m unittest discover -s tests -v
```
