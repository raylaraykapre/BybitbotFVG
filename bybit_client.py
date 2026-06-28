"""
Pure-Python Bybit V5 REST client.

No third-party dependencies. Uses only the Python standard library
(urllib, hmac, hashlib, json). Works on Termux and any Linux box that
ships a normal CPython install.

Supports Bybit "Demo Trading" by pointing at https://api-demo.bybit.com.
API keys must be generated from inside the Demo Trading account.

Docs reference: https://bybit-exchange.github.io/docs/v5/intro
"""

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request


MAINNET_HOST = "https://api.bybit.com"
DEMO_HOST = "https://api-demo.bybit.com"
TESTNET_HOST = "https://api-testnet.bybit.com"


class BybitError(Exception):
    """Raised when the Bybit API returns a non-zero retCode."""

    def __init__(self, ret_code, ret_msg, payload=None):
        self.ret_code = ret_code
        self.ret_msg = ret_msg
        self.payload = payload
        super().__init__("Bybit API error %s: %s" % (ret_code, ret_msg))


class BybitClient:
    def __init__(self, api_key, api_secret, demo=True, testnet=False,
                 recv_window=20000, logger=None):
        self.api_key = (api_key or "").strip()
        self.api_secret = (api_secret or "").strip()
        self.recv_window = int(recv_window)
        self.logger = logger

        if testnet:
            self.host = TESTNET_HOST
        elif demo:
            self.host = DEMO_HOST
        else:
            self.host = MAINNET_HOST

        # Difference (ms) between Bybit server clock and local clock.
        # Keeping this in sync is what prevents "invalid api / timestamp"
        # (retCode 10002) errors that otherwise make a valid key look invalid.
        self._time_offset_ms = 0
        self._last_sync = 0.0

    # ------------------------------------------------------------------ #
    # logging helper
    # ------------------------------------------------------------------ #
    def _log(self, msg):
        if self.logger:
            self.logger.debug(msg)

    # ------------------------------------------------------------------ #
    # time sync
    # ------------------------------------------------------------------ #
    def sync_time(self, force=False):
        """Sync local clock offset with Bybit server time."""
        now = time.time()
        if not force and (now - self._last_sync) < 60:
            return
        try:
            resp = self._raw_request("GET", "/v5/market/time", auth=False)
            # V5 returns time in result.timeNano / result.timeSecond
            result = resp.get("result", {})
            server_ms = None
            if result.get("timeNano"):
                server_ms = int(int(result["timeNano"]) / 1_000_000)
            elif result.get("timeSecond"):
                server_ms = int(result["timeSecond"]) * 1000
            elif resp.get("time"):
                server_ms = int(resp["time"])
            if server_ms:
                local_ms = int(time.time() * 1000)
                self._time_offset_ms = server_ms - local_ms
                self._last_sync = now
                self._log("time synced, offset=%dms" % self._time_offset_ms)
        except Exception as exc:  # noqa: BLE001
            self._log("time sync failed: %s" % exc)

    def _timestamp(self):
        return str(int(time.time() * 1000) + self._time_offset_ms)

    # ------------------------------------------------------------------ #
    # signing
    # ------------------------------------------------------------------ #
    def _sign(self, payload):
        return hmac.new(
            self.api_secret.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    # ------------------------------------------------------------------ #
    # low level request
    # ------------------------------------------------------------------ #
    def _raw_request(self, method, path, params=None, auth=False, timeout=15):
        params = params or {}
        url = self.host + path
        body = ""
        headers = {"Content-Type": "application/json"}

        if method == "GET":
            query = urllib.parse.urlencode(params)
            if query:
                url = url + "?" + query
            sign_payload_extra = query
            data = None
        else:
            body = json.dumps(params, separators=(",", ":")) if params else ""
            sign_payload_extra = body
            data = body.encode("utf-8")

        if auth:
            ts = self._timestamp()
            recv = str(self.recv_window)
            to_sign = ts + self.api_key + recv + sign_payload_extra
            sign = self._sign(to_sign)
            headers.update({
                "X-BAPI-API-KEY": self.api_key,
                "X-BAPI-SIGN": sign,
                "X-BAPI-SIGN-TYPE": "2",
                "X-BAPI-TIMESTAMP": ts,
                "X-BAPI-RECV-WINDOW": recv,
            })

        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                return json.loads(raw)
            except ValueError:
                raise BybitError(exc.code, raw)
        except urllib.error.URLError as exc:
            raise BybitError(-1, "network error: %s" % exc.reason)

        try:
            return json.loads(raw)
        except ValueError:
            raise BybitError(-1, "invalid JSON response: %s" % raw[:200])

    def request(self, method, path, params=None, auth=False, retries=2):
        """Request with retCode handling, retry, and auto time re-sync."""
        if auth:
            self.sync_time()

        attempt = 0
        while True:
            resp = self._raw_request(method, path, params=params, auth=auth)
            ret_code = resp.get("retCode", -1)
            if ret_code == 0:
                return resp

            ret_msg = resp.get("retMsg", "")
            # 10002 = timestamp / recv_window expired -> resync and retry
            # 10004 = sign error -> resync clock then retry once
            if ret_code in (10002, 10004) and attempt < retries:
                self._log("retCode %s (%s) -> resync + retry" %
                          (ret_code, ret_msg))
                self.sync_time(force=True)
                attempt += 1
                time.sleep(0.5)
                continue
            if attempt < retries and ret_code in (10006, 10016, 170007):
                # rate limit / server busy / timeout -> backoff retry
                attempt += 1
                time.sleep(1.0 * attempt)
                continue
            raise BybitError(ret_code, ret_msg, resp)

    # ------------------------------------------------------------------ #
    # public market endpoints
    # ------------------------------------------------------------------ #
    def get_server_time(self):
        return self.request("GET", "/v5/market/time")

    def get_kline(self, category, symbol, interval, limit=200):
        """Return list of candles, oldest first.

        Each candle: dict with start(ms), open, high, low, close, volume.
        Bybit returns newest-first; we reverse to oldest-first.
        """
        resp = self.request("GET", "/v5/market/kline", params={
            "category": category,
            "symbol": symbol,
            "interval": str(interval),
            "limit": int(limit),
        })
        rows = resp.get("result", {}).get("list", [])
        candles = []
        for row in rows:
            candles.append({
                "start": int(row[0]),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
            })
        candles.sort(key=lambda c: c["start"])
        return candles

    def get_tickers(self, category, symbol):
        resp = self.request("GET", "/v5/market/tickers", params={
            "category": category,
            "symbol": symbol,
        })
        lst = resp.get("result", {}).get("list", [])
        return lst[0] if lst else {}

    def get_last_price(self, category, symbol):
        t = self.get_tickers(category, symbol)
        if t and t.get("lastPrice"):
            return float(t["lastPrice"])
        return None

    def get_instrument_info(self, category, symbol):
        resp = self.request("GET", "/v5/market/instruments-info", params={
            "category": category,
            "symbol": symbol,
        })
        lst = resp.get("result", {}).get("list", [])
        return lst[0] if lst else {}

    def get_instruments_page(self, category, limit=1000, cursor=None):
        params = {"category": category, "limit": int(limit)}
        if cursor:
            params["cursor"] = cursor
        resp = self.request("GET", "/v5/market/instruments-info",
                            params=params)
        return resp.get("result", {})

    def get_all_instruments(self, category, status="Trading",
                            quote_coin=None, contract_type="LinearPerpetual"):
        """Return list of instrument dicts for a category, following cursor."""
        out = []
        cursor = None
        while True:
            result = self.get_instruments_page(category, limit=1000,
                                                cursor=cursor)
            for it in result.get("list", []):
                if status and it.get("status") != status:
                    continue
                if quote_coin and it.get("quoteCoin") != quote_coin:
                    continue
                if contract_type and it.get("contractType") != contract_type:
                    continue
                out.append(it)
            cursor = result.get("nextPageCursor")
            if not cursor:
                break
        return out

    def get_all_tickers(self, category):
        """Return all tickers for a category in a single request."""
        resp = self.request("GET", "/v5/market/tickers", params={
            "category": category,
        })
        return resp.get("result", {}).get("list", [])

    # ------------------------------------------------------------------ #
    # private account endpoints
    # ------------------------------------------------------------------ #
    def get_wallet_balance(self, account_type="UNIFIED", coin=None):
        params = {"accountType": account_type}
        if coin:
            params["coin"] = coin
        return self.request("GET", "/v5/account/wallet-balance",
                            params=params, auth=True)

    def get_coin_balance(self, coin="USDT", account_type="UNIFIED"):
        """Return (walletBalance, availableBalance) floats for a coin.

        Reads the per-coin figures first, then falls back to the
        account-level totals so sizing always sees the *whole* wallet.
        """
        resp = self.get_wallet_balance(account_type=account_type, coin=coin)
        lst = resp.get("result", {}).get("list", [])
        if not lst:
            return 0.0, 0.0
        account = lst[0]

        wallet = 0.0
        avail = 0.0
        for c in account.get("coin", []):
            if c.get("coin") == coin:
                wallet = _to_float(c.get("walletBalance"))
                # Candidate "free" figures, in order of preference.
                for key in ("availableToWithdraw", "free",
                            "availableBalance", "transferBalance"):
                    val = _to_float(c.get(key))
                    if val:
                        avail = val
                        break
                break

        # Account-level totals (UNIFIED reports these in USD terms).
        acct_wallet = _to_float(account.get("totalWalletBalance"))
        acct_avail = _to_float(account.get("totalAvailableBalance"))

        if wallet == 0.0 and acct_wallet:
            wallet = acct_wallet
        if avail == 0.0:
            avail = acct_avail or wallet
        return wallet, (avail or wallet)

    def get_positions(self, category, symbol=None, settle_coin=None):
        params = {"category": category}
        if symbol:
            params["symbol"] = symbol
        elif settle_coin:
            params["settleCoin"] = settle_coin
        resp = self.request("GET", "/v5/position/list", params=params,
                            auth=True)
        return resp.get("result", {}).get("list", [])

    def get_open_positions(self, category, symbol=None, settle_coin=None):
        positions = self.get_positions(category, symbol=symbol,
                                       settle_coin=settle_coin)
        return [p for p in positions if _to_float(p.get("size")) > 0]

    def set_leverage(self, category, symbol, leverage):
        lev = str(leverage)
        try:
            return self.request("POST", "/v5/position/set-leverage", params={
                "category": category,
                "symbol": symbol,
                "buyLeverage": lev,
                "sellLeverage": lev,
            }, auth=True)
        except BybitError as exc:
            # 110043 = leverage not modified (already set) -> treat as success
            if exc.ret_code == 110043:
                return {"retCode": 0, "retMsg": "leverage unchanged"}
            raise

    def place_market_order(self, category, symbol, side, qty,
                           take_profit=None, stop_loss=None,
                           position_idx=0, reduce_only=False):
        params = {
            "category": category,
            "symbol": symbol,
            "side": side,            # "Buy" or "Sell"
            "orderType": "Market",
            "qty": str(qty),
            "positionIdx": position_idx,
        }
        if take_profit is not None:
            params["takeProfit"] = str(take_profit)
        if stop_loss is not None:
            params["stopLoss"] = str(stop_loss)
        if take_profit is not None or stop_loss is not None:
            params["tpslMode"] = "Full"
        if reduce_only:
            params["reduceOnly"] = True
        return self.request("POST", "/v5/order/create", params=params,
                            auth=True)

    def set_trading_stop(self, category, symbol, take_profit=None,
                         stop_loss=None, position_idx=0):
        params = {
            "category": category,
            "symbol": symbol,
            "tpslMode": "Full",
            "positionIdx": position_idx,
        }
        if take_profit is not None:
            params["takeProfit"] = str(take_profit)
        if stop_loss is not None:
            params["stopLoss"] = str(stop_loss)
        return self.request("POST", "/v5/position/trading-stop",
                            params=params, auth=True)

    def request_demo_funds(self, coin="USDT", amount="100000"):
        """Top up demo trading wallet. Only valid on the demo host."""
        return self.request("POST", "/v5/account/demo-apply-money", params={
            "adjustType": 0,
            "utaDemoApplyMoney": [{"coin": coin, "amountStr": str(amount)}],
        }, auth=True)


def _to_float(value):
    try:
        if value in (None, ""):
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0
