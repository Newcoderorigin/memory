from __future__ import annotations

import csv
import importlib
import importlib.util
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from .market_models import Candle


class IngestionError(ValueError):
    """Raised when ingesting market data fails."""


_CANDLE_LINE = re.compile(
    r"(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})"
    r"\s+O:(?P<open>-?\d+(?:\.\d+)?)"
    r"\s+H:(?P<high>-?\d+(?:\.\d+)?)"
    r"\s+L:(?P<low>-?\d+(?:\.\d+)?)"
    r"\s+C:(?P<close>-?\d+(?:\.\d+)?)"
    r"\s+V:(?P<volume>-?\d+(?:\.\d+)?)"
)


class CsvMarketDataSource:
    ALLOWED_SUFFIXES = {".csv"}
    MAX_FILE_BYTES = 15_000_000

    def load(self, file_path: str) -> list[Candle]:
        path = self._validate_path(file_path)
        candles: list[Candle] = []
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            required = {"timestamp", "open", "high", "low", "close", "volume"}
            if reader.fieldnames is None or set(reader.fieldnames) != required:
                raise IngestionError("CSV headers must exactly match: timestamp,open,high,low,close,volume")
            for row in reader:
                candles.append(_row_to_candle(row))

        if not candles:
            raise IngestionError("CSV file does not contain candle rows")
        return candles

    def _validate_path(self, file_path: str) -> Path:
        path = Path(file_path).expanduser().resolve()
        if path.suffix.lower() not in self.ALLOWED_SUFFIXES:
            raise IngestionError("only .csv files are allowed")
        if not path.exists() or not path.is_file():
            raise IngestionError("CSV path does not exist or is not a file")
        if path.stat().st_size > self.MAX_FILE_BYTES:
            raise IngestionError("CSV exceeds maximum allowed size")
        return path


class ScreenVideoMarketDataSource:
    ALLOWED_SUFFIXES = {".mp4", ".mov", ".mkv", ".avi"}
    MAX_FILE_BYTES = 1_000_000_000

    def load_from_video(self, file_path: str, sample_rate: int = 8) -> list[Candle]:
        if sample_rate < 1 or sample_rate > 300:
            raise IngestionError("sample_rate must be between 1 and 300")
        path = self._validate_path(file_path)

        cv2 = self._load_optional_module("cv2", "opencv-python")
        pytesseract = self._load_optional_module("pytesseract", "pytesseract")

        capture = cv2.VideoCapture(str(path))
        if not capture.isOpened():
            raise IngestionError("unable to open video stream")

        candles: dict[datetime, Candle] = {}
        frame_no = 0
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            frame_no += 1
            if frame_no % sample_rate != 0:
                continue
            grayscale = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            text = pytesseract.image_to_string(grayscale)
            for candle in _parse_candles_from_text(text):
                candles[candle.timestamp] = candle

        capture.release()
        if not candles:
            raise IngestionError(
                "no candle overlays recognized in video. Ensure frame text format: YYYY-MM-DD HH:MM:SS O: H: L: C: V:"
            )
        return [candles[key] for key in sorted(candles)]

    def _validate_path(self, file_path: str) -> Path:
        path = Path(file_path).expanduser().resolve()
        if path.suffix.lower() not in self.ALLOWED_SUFFIXES:
            raise IngestionError("video extension must be one of .mp4, .mov, .mkv, .avi")
        if not path.exists() or not path.is_file():
            raise IngestionError("video path does not exist or is not a file")
        if path.stat().st_size > self.MAX_FILE_BYTES:
            raise IngestionError("video exceeds maximum allowed size")
        return path

    @staticmethod
    def _load_optional_module(module_name: str, package_name: str):
        if importlib.util.find_spec(module_name) is None:
            raise IngestionError(f"missing dependency '{package_name}'. Install it before running video analysis")
        return importlib.import_module(module_name)


def _row_to_candle(row: dict[str, str]) -> Candle:
    try:
        timestamp = _parse_timestamp(row["timestamp"])
        return Candle(
            timestamp=timestamp,
            open=Decimal(row["open"]),
            high=Decimal(row["high"]),
            low=Decimal(row["low"]),
            close=Decimal(row["close"]),
            volume=Decimal(row["volume"]),
        )
    except Exception as exc:  # noqa: BLE001
        raise IngestionError(f"invalid CSV row: {row}") from exc


def _parse_candles_from_text(text: str) -> list[Candle]:
    candles: list[Candle] = []
    for match in _CANDLE_LINE.finditer(text):
        candles.append(
            Candle(
                timestamp=_parse_timestamp(match.group("ts")),
                open=Decimal(match.group("open")),
                high=Decimal(match.group("high")),
                low=Decimal(match.group("low")),
                close=Decimal(match.group("close")),
                volume=Decimal(match.group("volume")),
            )
        )
    return candles


def _parse_timestamp(value: str) -> datetime:
    normalized = value.strip().replace(" ", "T")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
