"""Entry point: python -m optionsbot [--config config.yaml] [--simulate]"""
from __future__ import annotations

import argparse
import logging

import uvicorn

from .bot import TradingBot
from .config import load_config
from .server import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="NSE options recommendation and paper-trading bot")
    parser.add_argument("--config", help="YAML config file (see config.example.yaml)")
    parser.add_argument("--simulate", action="store_true", help="use the synthetic market instead of live NSE data")
    parser.add_argument("--host", help="bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, help="port (default 8000)")
    parser.add_argument("--db", dest="db_path", help="SQLite file for paper trades")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(args.config, host=args.host, port=args.port, db_path=args.db_path)
    if args.simulate:
        cfg.data.provider = "simulated"
        if args.config is None:
            cfg.data.poll_seconds = 3.0
        if args.db_path is None:
            cfg.db_path = "data/paper_trades_sim.db"  # never mix synthetic trades into the live record

    bot = TradingBot(cfg)
    print(f"\n  Dashboard: http://{cfg.host}:{cfg.port}   (data: {bot.provider.name})\n")
    uvicorn.run(create_app(bot), host=cfg.host, port=cfg.port, log_level="warning")


if __name__ == "__main__":
    main()
