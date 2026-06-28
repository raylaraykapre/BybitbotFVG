#!/usr/bin/env python3
"""
API key diagnostic for BybitbotFVG (LIVE mode only).

The built-in DEMO mode needs no keys, so this only matters for `--live`.

Bybit has separate environments and a key only works on the one it was made
in. A retCode 10003 ("API key is invalid") means the key was created in a
different environment than the bot is calling. This script signs a
wallet-balance request against each host so you can see where your key works.

Usage:
    python3 check_api.py            # uses ./config.json
    python3 check_api.py my.json
"""

import json
import sys

from bybit_client import BybitClient, BybitError


HOSTS = [
    ("mainnet", "https://api.bybit.com"),
    ("demo",    "https://api-demo.bybit.com"),
    ("testnet", "https://api-testnet.bybit.com"),
]


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "config.json"
    with open(path, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)

    api = cfg.get("live_api") or cfg.get("api") or {}
    key = (api.get("api_key") or "").strip()
    sec = (api.get("api_secret") or "").strip()
    coin = cfg.get("currency", {}).get("settle_coin", "USDT")

    print("BybitbotFVG API check")
    if not key or key.startswith("YOUR_") or not sec \
            or sec.startswith("YOUR_"):
        print("No API key set. The DEMO mode needs none: run "
              "`python3 bot.py --demo`.")
        print("For LIVE, set live_api.api_key and live_api.api_secret in "
              "config.json.")
        return

    print("Testing your key across Bybit environments...")
    match = None
    for name, host in HOSTS:
        client = BybitClient(api_key=key, api_secret=sec, demo=False)
        client.host = host
        try:
            client.sync_time(force=True)
            client.get_wallet_balance(account_type="UNIFIED", coin=coin)
            print("  OK   %-8s %s" % (name, host))
            match = name
        except BybitError as exc:
            print("  fail %-8s retCode %s" % (name, exc.ret_code))

    if match == "mainnet":
        print("Your key is a LIVE mainnet key. Good for --live.")
    elif match:
        print("Your key works on Bybit %s, not mainnet." % match)
    else:
        print("Key not accepted anywhere. Check the key/secret, expiry, "
              "or IP whitelist.")


if __name__ == "__main__":
    main()
