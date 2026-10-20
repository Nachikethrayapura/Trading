"""Build a standalone, shareable replay of the dashboard (one HTML file, no server needed).

    python -m optionsbot.demo --days 3 --out demo.html                       # simulated session
    python -m optionsbot --record session.jsonl                               # record a live session...
    python -m optionsbot.demo --recording session.jsonl --backtest reports/backtest --out demo.html

The page inlines the dashboard's HTML, CSS and JS with the recorded states and replays them tick
by tick. With --backtest it also shows that backtest's results (summary.json + equity.csv).
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path

from .bot import TradingBot
from .config import load_config
from .data.simulated import SimulatedProvider
from .storage import Store

WEB = Path(__file__).parent / "web"
TICKS_PER_DAY = 75
PER_FRAME_DROP = ("equity_curve", "closed", "events", "stats")


def _round(obj):
    if isinstance(obj, float):
        return round(obj, 4 if abs(obj) < 10 else 2)
    if isinstance(obj, dict):
        return {k: _round(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_round(v) for v in obj]
    return obj


def record(days: int = 3, seed: int = 21) -> dict:
    cfg = load_config()
    cfg.data.provider = "simulated"
    cfg.data.sim_seed = seed
    bot = TradingBot(cfg, provider=SimulatedProvider(cfg), store=Store(":memory:"), alerts=False)
    frames = []
    for _ in range(days * TICKS_PER_DAY):
        state = bot.step()
        frames.append({k: v for k, v in state.items() if k not in PER_FRAME_DROP})
    first_trade = next((i for i, f in enumerate(frames) if f["positions"]), 0)
    return _round({
        "frames": frames,
        "equity": bot.store.equity_curve(limit=10**6),
        "closed": list(reversed(bot.broker.closed_positions(limit=10**6))),  # oldest first
        "events": list(bot.events),  # newest first
        "start_index": first_trade,
    })


def from_recording(path: Path) -> dict:
    """Frames written by `python -m optionsbot --record`, plus the latest full state beside them."""
    frames = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if not frames:
        raise SystemExit(f"{path} has no recorded states")
    final = json.loads(Path(f"{path}.final.json").read_text())
    return _round({
        "frames": frames, "equity": final.get("equity_curve", []),
        "closed": list(reversed(final.get("closed", []))), "events": final.get("events", []),
        "start_index": 0,
    })


def load_backtest(directory: Path, max_points: int = 500) -> dict:
    """Summary and a downsampled equity curve from `python -m optionsbot.backtest --out DIR`."""
    summary = json.loads((directory / "summary.json").read_text())
    with open(directory / "equity.csv", newline="") as fh:
        curve = [{"ts": r["ts"], "equity": float(r["equity"])} for r in csv.DictReader(fh)]
    step = max(1, len(curve) // max_points)
    summary["equity"] = curve[::step] + ([curve[-1]] if curve and (len(curve) - 1) % step else [])
    validation = directory / "validation.json"
    if validation.exists():
        summary["validation"] = json.loads(validation.read_text())
    return _round({k: v for k, v in summary.items() if k != "errors"})


def build_html(recording: dict) -> str:
    """Artifact-ready page: content only (the host adds doctype/head/body), assets inlined."""
    index = (WEB / "index.html").read_text()
    body = re.search(r"<body>(.*)</body>", index, re.S).group(1)
    body = re.sub(r"<script[^>]*></script>\s*", "", body)
    payload = json.dumps(recording, separators=(",", ":")).replace("</", "<\\/")
    return "\n".join([
        "<title>NSE Options Bot</title>",
        f"<style>\n{(WEB / 'style.css').read_text()}</style>",
        body.strip(),
        f'<script id="demo-data" type="application/json">{payload}</script>',
        f"<script>\n{(WEB / 'demo.js').read_text()}</script>",
        f"<script>\n{(WEB / 'app.js').read_text()}</script>",
    ])


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a standalone replay of the dashboard")
    parser.add_argument("--days", type=int, default=3, help="simulated trading days to record")
    parser.add_argument("--seed", type=int, default=21)
    parser.add_argument("--recording", type=Path, help="JSONL from `python -m optionsbot --record` (instead of simulating)")
    parser.add_argument("--backtest", type=Path, help="backtest output directory to show on the page")
    parser.add_argument("--out", type=Path, default=Path("demo.html"))
    args = parser.parse_args()
    recording = from_recording(args.recording) if args.recording else record(args.days, args.seed)
    if args.backtest:
        recording["backtest"] = load_backtest(args.backtest)
    args.out.write_text(build_html(recording))
    print(f"Wrote {args.out} ({args.out.stat().st_size / 1e6:.1f} MB, {len(recording['frames'])} ticks, "
          f"{len(recording['closed'])} closed trades)")


if __name__ == "__main__":
    main()
