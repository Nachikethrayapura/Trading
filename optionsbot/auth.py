"""Daily broker login: exchanges the OAuth redirect for an access token and saves it.

    python -m optionsbot.auth kite      # needs KITE_API_KEY, KITE_API_SECRET
    python -m optionsbot.auth upstox    # needs UPSTOX_API_KEY, UPSTOX_API_SECRET, UPSTOX_REDIRECT_URI

Open the printed URL, log in, and paste the URL your browser is redirected to (or just the
request_token / code). The token is saved to data/broker_tokens.json (readable only by you) and
expires the next morning, so run this once per trading day.
"""
from __future__ import annotations

import argparse
import hashlib
import os
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from .data.brokers import KITE_BASE, UPSTOX_BASE, save_token


def _param_from(pasted: str, name: str) -> str:
    pasted = pasted.strip()
    if "://" in pasted or "?" in pasted:
        values = parse_qs(urlparse(pasted).query).get(name)
        if not values:
            raise SystemExit(f"No '{name}' parameter in that URL")
        return values[0]
    return pasted


def _env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(f"Set {name} first (from your broker's developer console)")
    return value


def kite_checksum(api_key: str, request_token: str, api_secret: str) -> str:
    return hashlib.sha256(f"{api_key}{request_token}{api_secret}".encode()).hexdigest()


def kite_login(pasted: str | None = None) -> str:
    api_key, api_secret = _env("KITE_API_KEY"), _env("KITE_API_SECRET")
    if pasted is None:
        print(f"1. Open: https://kite.zerodha.com/connect/login?v=3&api_key={api_key}")
        pasted = input("2. Paste the redirect URL (or request_token): ")
    request_token = _param_from(pasted, "request_token")
    resp = requests.post(f"{KITE_BASE}/session/token", headers={"X-Kite-Version": "3"}, timeout=15, data={
        "api_key": api_key, "request_token": request_token,
        "checksum": kite_checksum(api_key, request_token, api_secret)})
    body = resp.json()
    if resp.status_code != 200 or body.get("status") != "success":
        raise SystemExit(f"Kite login failed: {body.get('message') or resp.text[:200]}")
    token = body["data"]["access_token"]
    save_token("kite", token)
    return token


def upstox_login(pasted: str | None = None) -> str:
    client_id, secret = _env("UPSTOX_API_KEY"), _env("UPSTOX_API_SECRET")
    redirect = _env("UPSTOX_REDIRECT_URI")
    if pasted is None:
        query = urlencode({"response_type": "code", "client_id": client_id, "redirect_uri": redirect})
        print(f"1. Open: {UPSTOX_BASE}/login/authorization/dialog?{query}")
        pasted = input("2. Paste the redirect URL (or code): ")
    code = _param_from(pasted, "code")
    resp = requests.post(f"{UPSTOX_BASE}/login/authorization/token", timeout=15,
                         headers={"Accept": "application/json"},
                         data={"code": code, "client_id": client_id, "client_secret": secret,
                               "redirect_uri": redirect, "grant_type": "authorization_code"})
    body = resp.json()
    if resp.status_code != 200 or "access_token" not in body:
        raise SystemExit(f"Upstox login failed: {body.get('errors') or resp.text[:200]}")
    save_token("upstox", body["access_token"])
    return body["access_token"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate today's broker access token")
    parser.add_argument("broker", choices=["kite", "upstox"])
    parser.add_argument("redirect", nargs="?", help="redirect URL or token (skips the prompt)")
    args = parser.parse_args()
    (kite_login if args.broker == "kite" else upstox_login)(args.redirect)
    print(f"Saved today's {args.broker} token to data/broker_tokens.json. "
          f"Start the bot with: python -m optionsbot --provider {args.broker}")


if __name__ == "__main__":
    main()
