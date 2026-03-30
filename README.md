# Memory Data Bank

Secure local memory data bank implemented for Python 3.13 with:

- API key authentication (scrypt-hashed key verification)
- HMAC payload signatures for integrity verification
- SQLite persistence with WAL and strict parameterized queries
- Layered architecture (config/security/repository/service/cli)
- Market screen/video AI analysis command with secure local ingestion pipeline

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

## Market AI Analysis (CSV / Video Overlay)

Analyze chart data from either:

1. Structured CSV (`timestamp,open,high,low,close,volume`) or
2. Video overlays containing candle text in this OCR format:
   `YYYY-MM-DD HH:MM:SS O:<open> H:<high> L:<low> C:<close> V:<volume>`

### CSV example

```bash
memory-bank market-analyze --symbol BTC-USD --timeframe 1m --csv-path ./data/btc_1m.csv
```

### Video example

```bash
memory-bank market-analyze --symbol BTC-USD --timeframe 1m --video-path ./captures/chart.mp4 --sample-rate 8
```

The command returns JSON with:

- Support/resistance zones
- Signal (`BUY` / `SELL` / `HOLD`)
- Confidence score
- Stop loss / take profit levels
- Indicator rationale (EMA, RSI, MACD, ATR)

> Model output is informational only and must be validated with your own risk controls.

## Testing

```bash
python -m unittest discover -s tests -v
```
