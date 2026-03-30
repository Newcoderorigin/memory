from __future__ import annotations

import argparse
import json
from decimal import Decimal

from .config import DataBankConfig
from .market_analysis import MarketAnalysisEngine
from .market_ingestion import CsvMarketDataSource, ScreenVideoMarketDataSource
from .security import derive_key, hash_api_key
from .service import MemoryDataBankService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Secure local memory data bank")
    sub = parser.add_subparsers(dest="command", required=True)

    bootstrap = sub.add_parser("bootstrap", help="generate secure configuration values")
    bootstrap.add_argument("--api-key", required=True)
    bootstrap.add_argument("--signing-secret", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db-path", required=True)
    common.add_argument("--api-key-hash", required=True)
    common.add_argument("--signing-key", required=True)
    common.add_argument("--api-key", required=True)

    init_cmd = sub.add_parser("init", parents=[common], help="initialize database schema")
    init_cmd.set_defaults(action="init")

    put_cmd = sub.add_parser("put", parents=[common], help="upsert a key/value pair")
    put_cmd.add_argument("--namespace", required=True)
    put_cmd.add_argument("--key", required=True)
    put_cmd.add_argument("--value", required=True)
    put_cmd.set_defaults(action="put")

    get_cmd = sub.add_parser("get", parents=[common], help="retrieve a value by key")
    get_cmd.add_argument("--namespace", required=True)
    get_cmd.add_argument("--key", required=True)
    get_cmd.set_defaults(action="get")

    list_cmd = sub.add_parser("list", parents=[common], help="list keys in namespace")
    list_cmd.add_argument("--namespace", required=True)
    list_cmd.set_defaults(action="list")

    market_cmd = sub.add_parser("market-analyze", help="analyze market data from CSV or video overlays")
    market_cmd.add_argument("--symbol", required=True)
    market_cmd.add_argument("--timeframe", required=True)
    market_cmd.add_argument("--csv-path")
    market_cmd.add_argument("--video-path")
    market_cmd.add_argument("--sample-rate", type=int, default=8)

    return parser


def service_from_args(args: argparse.Namespace) -> MemoryDataBankService:
    config = DataBankConfig.from_env(
        db_path=args.db_path,
        api_key_hash=args.api_key_hash,
        signing_key=args.signing_key,
    )
    return MemoryDataBankService(config)


def _format_decimal(value: Decimal) -> str:
    return format(value, "f")


def _execute_market_analysis(args: argparse.Namespace) -> dict[str, object]:
    if bool(args.csv_path) == bool(args.video_path):
        raise SystemExit("exactly one of --csv-path or --video-path is required")

    if args.csv_path:
        candles = CsvMarketDataSource().load(args.csv_path)
    else:
        candles = ScreenVideoMarketDataSource().load_from_video(args.video_path, sample_rate=args.sample_rate)

    report = MarketAnalysisEngine().analyze(candles=candles, symbol=args.symbol, timeframe=args.timeframe)
    return {
        "symbol": report.symbol,
        "timeframe": report.timeframe,
        "generated_at": report.generated_at.isoformat(),
        "last_price": _format_decimal(report.last_price),
        "support_zone": {
            "price": _format_decimal(report.support_zone.price),
            "reason": report.support_zone.reason,
        },
        "resistance_zone": {
            "price": _format_decimal(report.resistance_zone.price),
            "reason": report.resistance_zone.reason,
        },
        "decision": {
            "signal": report.decision.signal,
            "confidence": _format_decimal(report.decision.confidence),
            "stop_loss": _format_decimal(report.decision.stop_loss),
            "take_profit": _format_decimal(report.decision.take_profit),
            "rationale": list(report.decision.rationale),
            "advisory": "Model output is informational only and must be validated with your own risk controls.",
        },
    }


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "bootstrap":
        payload = {
            "api_key_hash": hash_api_key(args.api_key),
            "signing_key": derive_key(args.signing_secret),
        }
        print(json.dumps(payload, indent=2))
        return

    if args.command == "market-analyze":
        print(json.dumps(_execute_market_analysis(args), indent=2))
        return

    service = service_from_args(args)

    if args.command == "init":
        service.initialize()
        print("initialized")
    elif args.command == "put":
        service.store(
            api_key=args.api_key,
            namespace=args.namespace,
            key=args.key,
            value=args.value,
        )
        print("stored")
    elif args.command == "get":
        record = service.retrieve(api_key=args.api_key, namespace=args.namespace, key=args.key)
        if record is None:
            raise SystemExit("not found")
        print(record.value)
    elif args.command == "list":
        records = service.list_namespace(api_key=args.api_key, namespace=args.namespace)
        print(json.dumps([{"key": r.key, "updated_at": r.updated_at.isoformat()} for r in records], indent=2))


if __name__ == "__main__":
    main()
