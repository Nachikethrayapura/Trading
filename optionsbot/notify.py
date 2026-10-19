"""Telegram alerts for entries, exits, circuit breakers, recommendations and data outages.

Setup:
  1. In Telegram, message @BotFather, send /newbot, copy the token it gives you.
  2. Send any message to your new bot, then run:  python -m optionsbot.notify --find-chat
  3. export TELEGRAM_BOT_TOKEN=...  TELEGRAM_CHAT_ID=...   (or put them under `alerts:` in config.yaml)
  4. python -m optionsbot.notify "test"   sends a test message.
"""
from __future__ import annotations

import argparse
import logging
import os
import queue
import threading
import time as _time

import requests

from .config import AlertsConfig, load_config

log = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{token}/{method}"


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, timeout: float = 10.0) -> None:
        self.token = token
        self.chat_id = chat_id
        self.timeout = timeout
        self.session = requests.Session()
        self._queue: queue.Queue[str] = queue.Queue(maxsize=200)
        self._last_sent: dict[str, float] = {}
        threading.Thread(target=self._worker, name="telegram", daemon=True).start()

    def send(self, text: str, key: str | None = None, cooldown: float = 0.0) -> bool:
        """Queue a message. With a key, repeats within `cooldown` seconds are dropped."""
        if key:
            now = _time.monotonic()
            if now - self._last_sent.get(key, -1e18) < cooldown:
                return False
            self._last_sent[key] = now
        try:
            self._queue.put_nowait(text[:4000])
            return True
        except queue.Full:
            log.warning("Telegram queue full; dropping alert")
            return False

    def send_now(self, text: str) -> None:
        resp = self.session.post(API.format(token=self.token, method="sendMessage"), timeout=self.timeout,
                                 json={"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True})
        if resp.status_code != 200:
            raise RuntimeError(f"Telegram error {resp.status_code}: {resp.text[:200]}")

    def _worker(self) -> None:
        while True:
            text = self._queue.get()
            for attempt in range(3):
                try:
                    self.send_now(text)
                    break
                except (requests.RequestException, RuntimeError) as exc:
                    log.warning("Telegram send failed (attempt %d): %s", attempt + 1, exc)
                    _time.sleep(2 ** attempt)


def make_notifier(cfg: AlertsConfig) -> TelegramNotifier | None:
    token = cfg.telegram_bot_token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = str(cfg.telegram_chat_id or os.environ.get("TELEGRAM_CHAT_ID", ""))
    if not cfg.enabled or not token or not chat_id:
        return None
    return TelegramNotifier(token, chat_id)


def find_chat_ids(token: str) -> list[tuple[str, str]]:
    resp = requests.get(API.format(token=token, method="getUpdates"), timeout=10)
    resp.raise_for_status()
    chats = {}
    for update in resp.json().get("result", []):
        chat = (update.get("message") or update.get("channel_post") or {}).get("chat") or {}
        if "id" in chat:
            chats[str(chat["id"])] = chat.get("title") or chat.get("username") or chat.get("first_name", "")
    return list(chats.items())


def main() -> None:
    parser = argparse.ArgumentParser(description="Telegram alert setup helper")
    parser.add_argument("message", nargs="?", help="send this test message")
    parser.add_argument("--find-chat", action="store_true", help="list chats that have messaged your bot")
    parser.add_argument("--config")
    args = parser.parse_args()
    cfg = load_config(args.config).alerts
    token = cfg.telegram_bot_token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN (from @BotFather) first")
    if args.find_chat:
        chats = find_chat_ids(token)
        if not chats:
            print("No chats yet: send any message to your bot in Telegram, then rerun.")
        for chat_id, name in chats:
            print(f"TELEGRAM_CHAT_ID={chat_id}   ({name})")
        return
    notifier = make_notifier(cfg)
    if notifier is None:
        raise SystemExit("Set TELEGRAM_CHAT_ID too (python -m optionsbot.notify --find-chat)")
    notifier.send_now(args.message or "NSE options bot: test alert")
    print("Sent.")


if __name__ == "__main__":
    main()
