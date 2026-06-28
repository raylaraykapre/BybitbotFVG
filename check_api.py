#!/usr/bin/env python3
"""
API key diagnostic for BybitbotFVG.

Bybit has FOUR separate environments and an API key only works on the one
it was created in:

    mainnet        -> https://api.bybit.com         (real money)
    mainnet-demo   -> https://api-demo.bybit.com     (Demo Trading)  <-- bot default
    testnet        -> https://api-testnet.bybit.com
    testnet-demo   -> (demo inside testnet, not useful)

A retCode 10003 ("API key is invalid") almost always means the key was
created in a DIFFERENT environment than the one the bot is calling.

This script signs the same wallet-balance request against each host using
your config.json credentials and tells you which environment the key
belongs to, and exactly what to put in config.json.

Usage:
    python3 check_api.py            # uses ./config.json
    python3 check_api.py my.json
"""

import json
import sys

from bybit_client import BybitClient, BybitError


HOSTS = [
    ("mainnet",      "https://api.bybit.com",          {"demo": False}),
    ("mainnet-demo", "https://api-demo.bybit.com",      {"demo": True}),
    ("testnet",      "https://api-testnet.bybit.com",   {"testnet": True}),
]


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "config.json"
    with open(path, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)

    api = cfg["api"]
    key = (api.get("api_key") or "").strip()
    sec = (api.get("api_secret") or "").strip()
    coin = cfg.get("currency", {}).get("settle_coin", "USDT")

    print("=" * 64)
    print("BybitbotFVG API key diagnostic")
    print("=" * 64)
    if not key or key.startswith("YOUR_") or not sec or sec.startswith("YOUR_"):
        print("config.json still has placeholder keys. Edit api.api_key / "
              "api.api_secret first.")
        return

    # surface common copy/paste problems
    raw_key = api.get("api_key", "")
    raw_sec = api.get("api_secret", "")
    if raw_key != raw_key.strip() or raw_sec != raw_sec.strip():
        print("! Warning: your key/secret had leading/trailing spaces "
              "(the bot trims them, but double-check the values).")
    print("Key length: %d   Secret length: %d" % (len(key), len(sec)))
    print("Testing this key against each Bybit environment...\n")

    match = None
    for name, host, _flags in HOSTS:
        client = BybitClient(api_key=key, api_secret=sec, demo=False)
        client.host = host
        try:
            client.sync_time(force=True)
            resp = client.get_wallet_balance(account_type="UNIFIED", coin=coin)
            ok = resp.get("retCode") == 0
            print("  [OK ] %-13s %s  -> key is VALID here" % (name, host))
            match = (name, host)
        except BybitError as exc:
            tag = {
                10003: "key not valid for this env",
                10004: "signature error (clock/secret?)",
                10005: "valid key but missing permission",
                33004: "key expired",
            }.get(exc.ret_code, exc.ret_msg)
            print("  [ -- ] %-13s %s  -> retCode %s (%s)" %
                  (name, host, exc.ret_code, tag))

    print("\n" + "-" * 64)
    if match is None:
        print("The key was not accepted on ANY environment. Likely causes:")
        print("  * Wrong key/secret (re-copy both from Bybit).")
        print("  * Key was deleted or expired.")
        print("  * Key has an IP whitelist that excludes this device's IP.")
        print("  * Your device clock is far off (less likely; bot auto-syncs).")
    else:
        name, host = match
        print("This key belongs to: %s (%s)" % (name, host))
        if name == "mainnet-demo":
            print("Set in config.json: api.demo = true   (this is the default)")
        elif name == "mainnet":
            print("This is a REAL-MONEY mainnet key.")
            print("For the demo bot: create a key from inside Demo Trading,")
            print("or set api.demo = false to trade real funds (NOT advised).")
        elif name == "testnet":
            print("Set in config.json: api.testnet = true  (and api.demo=false)")
    print("-" * 64)
    print("How to create a DEMO key:")
    print("  1) Log in to your normal Bybit (mainnet) account.")
    print("  2) Switch to 'Demo Trading' (it is a separate account/user ID).")
    print("  3) While IN Demo Trading, open the API menu and create a key")
    print("     with Read + Trade (Unified Trading) permission.")
    print("  4) Put that key/secret in config.json with api.demo = true.")


if __name__ == "__main__":
    main()
