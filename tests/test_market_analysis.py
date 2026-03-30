from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from memory_bank.cli import _execute_market_analysis
from memory_bank.market_analysis import MarketAnalysisEngine, MarketAnalysisError
from memory_bank.market_ingestion import CsvMarketDataSource
from memory_bank.market_models import Candle


class MarketAnalysisTests(unittest.TestCase):
    def _build_candles(self, count: int = 60) -> list[Candle]:
        now = datetime(2025, 1, 1, tzinfo=UTC)
        candles: list[Candle] = []
        price = Decimal("100")
        for idx in range(count):
            open_price = price
            close = price + Decimal("0.3")
            high = close + Decimal("0.8")
            low = open_price - Decimal("0.4")
            candles.append(
                Candle(
                    timestamp=now + timedelta(minutes=idx),
                    open=open_price,
                    high=high,
                    low=low,
                    close=close,
                    volume=Decimal("1000") + Decimal(idx),
                )
            )
            price = close
        return candles

    def test_engine_generates_report(self) -> None:
        report = MarketAnalysisEngine().analyze(candles=self._build_candles(), symbol="BTC-USD", timeframe="1m")
        self.assertIn(report.decision.signal, {"BUY", "SELL", "HOLD"})
        self.assertGreaterEqual(report.decision.confidence, Decimal("0.51"))

    def test_engine_rejects_too_few_candles(self) -> None:
        with self.assertRaises(MarketAnalysisError):
            MarketAnalysisEngine().analyze(candles=self._build_candles(count=10), symbol="BTC-USD", timeframe="1m")

    def test_csv_ingestion_and_cli_execution(self) -> None:
        candles = self._build_candles()
        with tempfile.TemporaryDirectory() as tmp_dir:
            csv_path = Path(tmp_dir) / "candles.csv"
            with csv_path.open("w", encoding="utf-8") as handle:
                handle.write("timestamp,open,high,low,close,volume\n")
                for item in candles:
                    handle.write(
                        f"{item.timestamp.isoformat()},{item.open},{item.high},{item.low},{item.close},{item.volume}\n"
                    )

            source = CsvMarketDataSource().load(str(csv_path))
            self.assertEqual(len(source), 60)

            args = type(
                "Args",
                (),
                {
                    "csv_path": str(csv_path),
                    "video_path": None,
                    "symbol": "BTC-USD",
                    "timeframe": "1m",
                    "sample_rate": 8,
                },
            )
            payload = _execute_market_analysis(args)
            self.assertEqual(payload["symbol"], "BTC-USD")
            self.assertIn(payload["decision"]["signal"], {"BUY", "SELL", "HOLD"})
            json.dumps(payload)


if __name__ == "__main__":
    unittest.main()
