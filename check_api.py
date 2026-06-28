#!/usr/bin/env python3
"""
API key diagnostic for BybitbotFVG.

Bybit has FOUR separate environments and an API key only works on the one
it was created in:

    mainnet        -> https://api.bybit.com          (real money)
    mainnet-demo   -> https://api-demo.bybit.com      (Demo Trading)
    testnet        -> https://api-testnet.bybit.com
    testnet-demo   -> (demo inside testnet, not useful)

A retCode 10003 ("API key is invalid") almost always means the key was
created in a DIFFERENT environment than the one the bot is calling.

This script reads every credential pair you configured (demo / mainnet /
testnet) and signs a wallet-balance request against each host, so you can see
exactly which environment each key belongs to.

Usage:
    python3 check_api.py            # uses ./config.json
    python3 check_api.py my.json
"""

import json
import sys

from bybit_client import BybitClient, BybitError


HOSTS = [
    ("mainnet",      "https://api.bybit.com"),
    ("mainnet-demo", "https://api-demo.bybit.com"),
    ("testnet",      "https://api-testnet.bybit.com"),
]


def collect_credentials(api):
    """Return list of (label, key, secret) for every configured, non-empty,
    non-placeholder credential pair."""
    creds = []
    pairs = [
        ("demo",    "demo_api_key",    "demo_api_secret"),
        ("mainnet", "mainnet_api_key", "mainnet_api_secret"),
        ("testnet", "testnet_api_key", "testnet_api_secret"),
    ]
    for label, kk, sk in pairs:
        key = (api.get(kk) or "").strip()
        sec = (api.get(sk) or "").strip()
        if key and sec and not key.startswith("YOUR_") \
                and not sec.startswith("YOUR_"):
            creds.append((label, key, sec))
    # legacy flat keys
    legacy_k = (api.get("api_key") or "").strip()
    legacy_s = (api.get("api_secret") or "").strip()
    if (not creds and legacy_k and legacy_s
            and not legacy_k.startswith("YOUR_")):
        creds.append(("(legacy)", legacy_k, legacy_s))
    return creds


def test_credential(label, key, sec, coin):
    print("\n>>> Testing '%s' key (len key=%d, secret=%d)" %
          (label, len(key), len(sec)))
    match = None
    for name, host in HOSTS:
        client = BybitClient(api_key=key, api_secret=sec, demo=False)
        client.host = host
        try:
            client.sync_time(force=True)
            client.get_wallet_balance(account_type="UNIFIED", coin=coin)
            print("   [OK ] %-13s %s  -> VALID here" % (name, host))
            match = (name, host)
        except BybitError as exc:
            tag = {
                10003: "key not valid for this env",
                10004: "signature error (clock/secret?)",
                10005: "valid key but missing permission",
                33004: "key expired",
            }.get(exc.ret_code, exc.ret_msg)
            print("   [ - ] %-13s %s  -> retCode %s (%s)" %
                  (name, host, exc.ret_code, tag))
    if match:
        name, _ = match
        cfg_hint = {
            "mainnet": 'set api.live_environment = "mainnet" (REAL money)',
            "mainnet-demo": 'set api.live_environment = "demo"',
            "testnet": 'set api.live_environment = "testnet"',
        }.get(name, "")
        print("   => '%s' key belongs to %s. %s" % (label, name, cfg_hint))
    else:
        print("   => '%s' key was not accepted on any environment." % label)
    return match


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "config.json"
    with open(path, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)

    api = cfg.get("api", {})
    coin = cfg.get("currency", {}).get("settle_coin", "USDT")

    print("=" * 64)
    print("BybitbotFVG API key diagnostic")
    print("=" * 64)

    creds = collect_credentials(api)
    if not creds:
        print("No real API keys configured yet. Edit config.json -> api:")
        print("  demo_api_key / demo_api_secret      (Bybit Demo account)")
        print("  mainnet_api_key / mainnet_api_secret (real money)")
        print("\nReminder: the built-in DEMO mode (mode=paper / --demo) needs")
        print("NO keys at all - just run `python3 bot.py --demo`.")
        return

    for label, key, sec in creds:
        test_credential(label, key, sec, coin)

    print("\n" + "-" * 64)
    print("How to create keys:")
    print("  * DEMO account key: log in to Bybit, switch to 'Demo Trading'")
    print("    (separate account/user ID), then create an API key there.")
    print("  * LIVE key: create it on your normal mainnet account.")
    print("Then put each in its matching slot in config.json and choose")
    print("api.live_environment accordingly. (Built-in paper demo needs none.)")


if __name__ == "__main__":
    main()
