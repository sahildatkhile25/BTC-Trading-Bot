import csv
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    print("[ERROR] Missing package: requests. Install it with: py -m pip install requests", flush=True)
    raise


# =====================================================
# CONFIG
# =====================================================

BASE_URL = "https://api.india.delta.exchange"

API_KEY = os.getenv("DELTA_API_KEY", "Your_API_key_Here")
API_SECRET = os.getenv("DELTA_API_SECRET", "Your_API_Secerete_Here")

CAPITAL = 100000
TARGET_PERCENT = 2.0
RSI_LENGTH = 11
RSI_MIN = 50.0
TIMEFRAME = "1m"
CANDLE_PRICE_SOURCE = "MARK"
LIVE_PRICE_SOURCE = "MARK"
LOOP_INTERVAL = 5
STATUS_INTERVAL = 15
LOG_FILE = "btc_option_trades.csv"

DRY_RUN = os.getenv("DRY_RUN", "1") == "1"
PAPER_ON_ORDER_FAIL = os.getenv("PAPER_ON_ORDER_FAIL", "1") == "1"

position = None
last_signal_id_by_symbol = {}

# =====================================================
# SESSION STATS  (UI only — no logic change)
# =====================================================

session_stats = {
    "total_trades": 0,
    "total_pnl":    0.0,
    "trade_log":    [],   # list of dicts per closed trade
}

# =====================================================
# ANSI COLOR / SYMBOL HELPERS  (UI only)
# =====================================================

RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"

# Text colors
RED    = "\033[31m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
CYAN   = "\033[36m"
WHITE  = "\033[97m"
ORANGE = "\033[38;5;208m"
BLUE   = "\033[34m"
GRAY   = "\033[90m"

# Background highlights
BG_GREEN  = "\033[42m"
BG_RED    = "\033[41m"
BG_YELLOW = "\033[43m"
BG_BLUE   = "\033[44m"
BG_CYAN   = "\033[46m"

DOT_RED   = f"{RED}●{RESET}"   # red dot
DOT_GREEN = f"{GREEN}●{RESET}" # green dot
DOT_AMBER = f"{YELLOW}●{RESET}"

TICK  = f"{GREEN}✔{RESET}"
CROSS = f"{RED}✘{RESET}"

SEP_THIN  = f"{GRAY}{'─' * 70}{RESET}"
SEP_THICK = f"{CYAN}{'═' * 70}{RESET}"

def _c(color, text):   return f"{color}{text}{RESET}"
def _b(text):          return f"{BOLD}{text}{RESET}"
def _bg(color, text):  return f"{color}{BOLD}{WHITE}{text}{RESET}"


# =====================================================
# LOGGING
# =====================================================

def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def now_time():
    return datetime.now().strftime("%H:%M:%S")


def log(tag, message):
    tag_colors = {
        "BOT":        CYAN,
        "STATUS":     BLUE,
        "TRADE_FOUND":YELLOW,
        "ORDER":      ORANGE,
        "PNL":        GREEN,
        "RESULT":     BOLD,
        "ERROR":      RED,
        "PAPER":      GRAY,
    }
    color = tag_colors.get(tag, WHITE)
    print(f"\n{_c(color, f'[{now_text()}] [{tag}]')}\n{message}\n", flush=True)


def fmt(value):
    if value is None:
        return "-"
    try:
        return f"{float(value):.2f}"
    except Exception:
        return str(value)


def yn(value):
    return f"{TICK} YES" if value else f"{CROSS} NO "


def side_of(contract):
    symbol = str(contract.get("symbol", "")).upper()
    contract_type = str(contract.get("contract_type", "")).lower()
    if contract_type == "call_options" or symbol.startswith("C-"):
        return "CALL"
    if contract_type == "put_options" or symbol.startswith("P-"):
        return "PUT"
    return "OPTION"


def status_unavailable(reason):
    return {"ok": False, "reason": reason}


def side_dot(side):
    """Green dot for CALL, red dot for PUT."""
    return DOT_GREEN if side == "CALL" else DOT_RED


def print_watch_status(status_by_key):
    if not status_by_key:
        log("STATUS", "Waiting for first CALL/PUT market data check.")
        return

    lines = [
        SEP_THICK,
        f"  {_b('WATCHLIST')}  {_c(GRAY, TIMEFRAME)}  {_c(DIM, f'candles:{CANDLE_PRICE_SOURCE}  price:{LIVE_PRICE_SOURCE}')}",
        SEP_THIN,
        f"  {'DOT':<4} {'SIDE':<5} {'SYMBOL':<24} {'SETUP':<7} {'LTP':>8} {'RSI':>6} {'QTY':>6}",
        SEP_THIN,
    ]

    def sort_key(item):
        key = item[0]
        if key.startswith("CALL"): return (0, key)
        if key.startswith("PUT"):  return (1, key)
        return (2, key)

    for key, status in sorted(status_by_key.items(), key=sort_key):
        side, symbol = key.split(" ", 1)
        dot = side_dot(side)

        if not status.get("ok"):
            lines.append(f"  {dot}  {side:<5} {symbol:<24} {_c(GRAY,'N/A'):<7}")
            _reason = status.get('reason', '-')
            lines.append(f"  {_c(GRAY, '  reason: ' + _reason)}")
            continue

        setup_flag = _bg(BG_GREEN, " READY ") if status["setup_ok"] else _c(GRAY, "waiting")
        ltp_str    = _c(CYAN, fmt(status["ltp"]))
        rsi_str    = _c(GREEN if status.get("rsi_ok") else GRAY, fmt(status["rsi"]))

        lines.append(
            f"  {dot}  {side:<5} {_b(symbol):<24} {setup_flag}  "
            f"{ltp_str:>8}  {rsi_str:>6}  {str(status['qty']):>6}"
        )
        lines.append(
            f"       "
            f"red={yn(status['red_ok'])}  "
            f"green={yn(status['green_ok'])}  "
            f"breakout={yn(status['breakout_ok'])}  "
            f"RSI>{RSI_MIN}={yn(status['rsi_ok'])}"
        )
        lines.append(
            f"       {_c(GRAY,'green_high=')} {_c(CYAN, fmt(status['green_high']))}  "
            f"{_c(GRAY,'SL=')} {_c(RED, fmt(status['stop_loss']))}  "
            f"{_c(GRAY,'target=')} {_c(GREEN, fmt(status['target_if_entry']))}"
        )
        if not status["setup_ok"]:
            lines.append(f"       {DOT_AMBER} {_c(YELLOW, status['reason'])}")

    lines.append(SEP_THICK)
    log("STATUS", "\n".join(lines))


def print_trade_found(side, symbol, setup, qty):
    red   = setup["red"]
    green = setup["green"]
    dot   = side_dot(side)

    lines = [
        SEP_THICK,
        f"  {dot}  {_bg(BG_YELLOW, f'  TRADE SIGNAL FOUND  ')}  {_b(symbol)}",
        SEP_THIN,
        f"  {_c(GRAY,'Side       :')} {_b(side)}",
        f"  {_c(GRAY,'Symbol     :')} {_b(_c(CYAN, symbol))}",
        f"  {_c(GRAY,'Timeframe  :')} {TIMEFRAME}",
        f"  {_c(GRAY,'Candles    :')} {CANDLE_PRICE_SOURCE}",
        f"  {_c(GRAY,'Live Price :')} {LIVE_PRICE_SOURCE}",
        SEP_THIN,
        f"  {_c(GRAY,'Entry      :')} {_b(_c(CYAN,   fmt(setup['entry'])))}",
        f"  {_c(GRAY,'Target     :')} {_b(_c(GREEN,  fmt(setup['target'])))}",
        f"  {_c(GRAY,'Stop Loss  :')} {_b(_c(RED,    fmt(setup['stop_loss'])))}",
        f"  {_c(GRAY,'Qty        :')} {_b(str(qty))}",
        f"  {_c(GRAY,f'RSI({RSI_LENGTH})    :')} {_c(GREEN, fmt(setup['rsi']))}",
        f"  {_c(GRAY,'Green High :')} {_c(CYAN, fmt(setup['green_high']))}",
        SEP_THIN,
        f"  {_c(GRAY,'CANDLES USED BY BOT')}",
        f"  {DOT_RED}  Red   O:{fmt(red['open'])} H:{_c(RED,fmt(red['high']))} L:{_c(RED,fmt(red['low']))} C:{fmt(red['close'])}",
        f"  {DOT_GREEN}  Green O:{fmt(green['open'])} H:{_c(GREEN,fmt(green['high']))} L:{_c(GREEN,fmt(green['low']))} C:{fmt(green['close'])}",
        SEP_THICK,
    ]
    log("TRADE_FOUND", "\n".join(lines))


def print_order(action, side, symbol, product_id, qty, price, dry_run=False, paper=False):
    mode = "PAPER" if paper else "DRY RUN" if dry_run else "LIVE"
    mode_color = GRAY if paper else YELLOW if dry_run else GREEN
    dot  = side_dot(side)
    action_color = GREEN if action.upper() == "BUY" else RED

    lines = [
        SEP_THIN,
        f"  {dot}  {_bg(action_color, f'  {action.upper()} ORDER  ')}  {_c(mode_color, f'[{mode}]')}",
        f"  {_c(GRAY,'Symbol     :')} {_b(symbol)}",
        f"  {_c(GRAY,'Product ID :')} {product_id}",
        f"  {_c(GRAY,'Qty        :')} {_b(str(qty))}",
        f"  {_c(GRAY,'Price      :')} {_b(_c(CYAN, fmt(price)))}",
        SEP_THIN,
    ]
    log("ORDER", "\n".join(lines))


def pnl_values(pos, ltp):
    pnl = round((ltp - pos["entry"]) * pos["qty"], 2)
    pnl_percent = round((pnl / CAPITAL) * 100, 2)
    return pnl, pnl_percent


def print_pnl_status(pos, ltp):
    pnl, pnl_percent = pnl_values(pos, ltp)
    dot  = side_dot(pos["side"])
    pnl_color  = GREEN if pnl >= 0 else RED
    status_str = _bg(BG_GREEN if pnl >= 0 else BG_RED, f"  {'PROFIT' if pnl >= 0 else 'LOSS'}  ")
    target_gap = round(pos["target"] - ltp, 2)
    stop_gap   = round(ltp - pos["stop_loss"], 2)
    mode_str   = _c(GRAY, "PAPER") if pos.get("paper") else _c(GREEN, "LIVE")

    lines = [
        SEP_THICK,
        f"  {dot}  {_b('LIVE PNL')}  {status_str}  {mode_str}",
        SEP_THIN,
        f"  {_c(GRAY,'Side       :')} {_b(pos['side'])}",
        f"  {_c(GRAY,'Symbol     :')} {_b(_c(CYAN, pos['symbol']))}",
        f"  {_c(GRAY,'Entry      :')} {_c(CYAN,  fmt(pos['entry']))}",
        f"  {_c(GRAY,'LTP        :')} {_b(_c(CYAN, fmt(ltp)))}",
        f"  {_c(GRAY,'Target     :')} {_c(GREEN, fmt(pos['target']))}  {_c(GRAY, f'gap {fmt(target_gap)}')}",
        f"  {_c(GRAY,'Stop Loss  :')} {_c(RED,   fmt(pos['stop_loss']))}  {_c(GRAY, f'gap {fmt(stop_gap)}')}",
        f"  {_c(GRAY,'Qty        :')} {pos['qty']}",
        f"  {_c(GRAY,'PnL        :')} {_b(_c(pnl_color, fmt(pnl)))}",
        f"  {_c(GRAY,'PnL %      :')} {_b(_c(pnl_color, f'{fmt(pnl_percent)}%'))}",
        SEP_THICK,
    ]
    log("PNL", "\n".join(lines))


def print_trade_result(reason, pos, exit_price):
    pnl, pnl_percent = pnl_values(pos, exit_price)
    result     = "PROFIT" if pnl >= 0 else "LOSS"
    dot        = side_dot(pos["side"])
    pnl_color  = GREEN if pnl >= 0 else RED
    res_banner = _bg(BG_GREEN if pnl >= 0 else BG_RED, f"  {result}  ")
    reason_color = GREEN if reason == "TARGET" else RED

    # update session stats
    session_stats["total_trades"] += 1
    session_stats["total_pnl"]    += pnl
    session_stats["trade_log"].append({
        "n":      session_stats["total_trades"],
        "side":   pos["side"],
        "symbol": pos["symbol"],
        "entry":  pos["entry"],
        "exit":   exit_price,
        "qty":    pos["qty"],
        "pnl":    pnl,
        "reason": reason,
        "time":   now_text(),
    })

    # session summary so far
    total_pnl_color = GREEN if session_stats["total_pnl"] >= 0 else RED

    lines = [
        SEP_THICK,
        f"  {dot}  {_b('TRADE RESULT')}  {res_banner}",
        SEP_THIN,
        f"  {_c(GRAY,'Mode       :')} {'PAPER' if pos.get('paper') else _c(GREEN,'LIVE')}",
        f"  {_c(GRAY,'Reason     :')} {_b(_c(reason_color, reason))}",
        f"  {_c(GRAY,'Side       :')} {_b(pos['side'])}",
        f"  {_c(GRAY,'Symbol     :')} {_b(_c(CYAN, pos['symbol']))}",
        f"  {_c(GRAY,'Entry      :')} {_c(CYAN, fmt(pos['entry']))}",
        f"  {_c(GRAY,'Exit       :')} {_b(_c(CYAN, fmt(exit_price)))}",
        f"  {_c(GRAY,'Time       :')} {_c(GRAY, now_text())}",
        f"  {_c(GRAY,'Qty        :')} {pos['qty']}",
        f"  {_c(GRAY,'PnL        :')} {_b(_c(pnl_color, fmt(pnl)))}",
        f"  {_c(GRAY,'PnL %      :')} {_b(_c(pnl_color, f'{fmt(pnl_percent)}%'))}",
        SEP_THIN,
        f"  {_b('SESSION SUMMARY')}",
        f"  {_c(GRAY,'Total trades:')} {_b(str(session_stats['total_trades']))}",
        f"  {_c(GRAY,'Total PnL   :')} {_b(_c(total_pnl_color, fmt(session_stats['total_pnl'])))}",
        SEP_THIN,
        f"  {_b('TRADE HISTORY')}",
    ]

    for t in session_stats["trade_log"]:
        tc = GREEN if t["pnl"] >= 0 else RED
        n_str  = str(t["n"])
        sym    = t["symbol"]
        reason = t["reason"]
        ts     = t["time"]
        side_s = t["side"]
        pnl_s  = fmt(t["pnl"])
        lines.append(
            f"  {_c(GRAY, "#" + n_str)}"
            f"  {side_dot(side_s)} {side_s:<5}"
            f"  {_c(CYAN, sym):<24}"
            f"  {reason:<9}"
            f"  PnL: {_b(_c(tc, pnl_s))}"
            f"  {_c(GRAY, ts)}"
        )

    lines.append(SEP_THICK)
    log("RESULT", "\n".join(lines))


def print_shutdown_summary():
    """Printed when user presses Ctrl+C — session totals + all trades."""
    total_pnl_color = GREEN if session_stats["total_pnl"] >= 0 else RED

    lines = [
        SEP_THICK,
        f"  {_b(_c(CYAN, 'SESSION ENDED'))}  {_c(GRAY, now_text())}",
        SEP_THIN,
        f"  {_c(GRAY,'Total trades :')} {_b(str(session_stats['total_trades']))}",
        f"  {_c(GRAY,'Total PnL    :')} {_b(_c(total_pnl_color, fmt(session_stats['total_pnl'])))}",
    ]

    if session_stats["trade_log"]:
        lines += [SEP_THIN, f"  {_b('ALL TRADES')}"]
        for t in session_stats["trade_log"]:
            tc      = GREEN if t["pnl"] >= 0 else RED
            n_str   = str(t["n"])
            sym     = t["symbol"]
            side_s  = t["side"]
            reason  = t["reason"]
            ts      = t["time"]
            pnl_s   = fmt(t["pnl"])
            entry_s = fmt(t["entry"])
            exit_s  = fmt(t["exit"])
            qty_s   = str(t["qty"])
            lines.append(
                f"  {_c(GRAY, '#' + n_str)}"
                f"  {side_dot(side_s)} {side_s:<5}"
                f"  {_c(CYAN, sym):<28}"
                f"  entry={_c(CYAN, entry_s)}"
                f"  exit={_c(CYAN, exit_s)}"
                f"  qty={qty_s}"
                f"  {reason:<9}"
                f"  PnL={_b(_c(tc, pnl_s))}"
                f"\n       {_c(GRAY, ts)}"
            )
    else:
        lines.append(f"  {_c(GRAY, 'No trades taken this session.')}")

    lines.append(SEP_THICK)
    print("\n" + "\n".join(lines) + "\n", flush=True)


# =====================================================
# API HELPERS
# =====================================================

def public_get(path, params=None):
    try:
        response = requests.get(BASE_URL + path, params=params, timeout=10)
        data = response.json()
        if not data.get("success"):
            return None
        return data.get("result")
    except Exception:
        return None


def signature(method, path, timestamp, body=""):
    message = method + timestamp + path + body
    return hmac.new(API_SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()


def place_order(product_id, side, qty):
    if DRY_RUN:
        return {
            "success": True,
            "dry_run": True,
            "product_id": product_id,
            "side": side,
            "size": int(qty),
        }

    path = "/v2/orders"
    timestamp = str(int(time.time()))
    payload = {
        "product_id": product_id,
        "size": int(qty),
        "side": side,
        "order_type": "market_order",
    }
    body = json.dumps(payload, separators=(",", ":"))
    headers = {
        "api-key": API_KEY,
        "timestamp": timestamp,
        "signature": signature("POST", path, timestamp, body),
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(BASE_URL + path, data=body, headers=headers, timeout=10)
        data = response.json()
        if not data.get("success"):
            log("ERROR", f"{side.upper()} order failed:\n{data}")
            return None
        return data
    except Exception as exc:
        log("ERROR", f"{side.upper()} order exception:\n{exc}")
        return None


# =====================================================
# MARKET DATA
# =====================================================

def get_btc_spot_price():
    for symbol in ["BTCUSD", "BTCUSDT", "BTC_USDT"]:
        ticker = public_get(f"/v2/tickers/{symbol}")
        if not ticker:
            continue
        price = ticker.get("spot_price") or ticker.get("close") or ticker.get("mark_price")
        try:
            price = float(price)
        except Exception:
            price = 0
        if price > 0:
            return price
    log("ERROR", "Could not fetch BTC spot price.\nCheck your internet connection first")
    return None


def parse_expiry(contract):
    value = contract.get("settlement_time") or contract.get("expiry") or contract.get("expiration")
    if value is None:
        return None
    try:
        if isinstance(value, str):
            expiry = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            expiry = datetime.fromtimestamp(int(value), tz=timezone.utc)
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return expiry.astimezone(timezone.utc)
    except Exception:
        return None


def get_strike(contract):
    strike = contract.get("strike_price") or contract.get("strike")
    if strike is not None:
        try:
            return float(strike)
        except Exception:
            pass
    symbol = str(contract.get("symbol", ""))
    for part in symbol.replace("_", "-").split("-"):
        try:
            value = float(part)
            if 1000 < value < 10000000:
                return value
        except Exception:
            continue
    return None


def find_atm_contracts():
    products = public_get("/v2/products")
    spot = get_btc_spot_price()
    if not products or spot is None:
        return None, None

    now_utc = datetime.now(timezone.utc)
    candidates = []

    for contract in products:
        symbol = str(contract.get("symbol", "")).upper()
        contract_type = str(contract.get("contract_type", "")).lower()
        if "BTC" not in symbol:
            continue
        if contract_type not in {"call_options", "put_options"}:
            continue
        expiry = parse_expiry(contract)
        strike = get_strike(contract)
        if expiry is None or strike is None:
            continue
        if expiry < now_utc:
            continue
        candidates.append({
            "contract": contract,
            "type": contract_type,
            "expiry": expiry,
            "strike": strike,
        })

    if not candidates:
        log("ERROR", "No live BTC options found.")
        return None, None

    nearest_expiry = min(item["expiry"].date() for item in candidates)
    same_expiry = [item for item in candidates if item["expiry"].date() == nearest_expiry]
    calls = [item for item in same_expiry if item["type"] == "call_options"]
    puts  = [item for item in same_expiry if item["type"] == "put_options"]
    atm_call = min(calls, key=lambda item: abs(item["strike"] - spot), default=None)
    atm_put  = min(puts,  key=lambda item: abs(item["strike"] - spot), default=None)

    return (
        atm_call["contract"] if atm_call else None,
        atm_put["contract"]  if atm_put  else None,
    )


def get_ltp(symbol):
    ticker = public_get(f"/v2/tickers/{symbol}")
    if not ticker:
        return None
    if LIVE_PRICE_SOURCE.upper() == "MARK":
        price = ticker.get("mark_price") or ticker.get("close") or ticker.get("spot_price")
    else:
        price = ticker.get("close") or ticker.get("mark_price") or ticker.get("spot_price")
    try:
        price = float(price)
    except Exception:
        return None
    return price if price > 0 else None


def get_candles(symbol):
    end_time   = int(time.time())
    start_time = end_time - 3600
    candle_symbol = f"MARK:{symbol}" if CANDLE_PRICE_SOURCE.upper() == "MARK" else symbol
    result = public_get(
        "/v2/history/candles",
        params={
            "symbol":     candle_symbol,
            "resolution": TIMEFRAME,
            "start":      start_time,
            "end":        end_time,
        },
    )
    if not result or len(result) < RSI_LENGTH + 3:
        return None
    candles = []
    for item in result:
        try:
            candles.append({
                "time":  int(item["time"]),
                "open":  float(item["open"]),
                "high":  float(item["high"]),
                "low":   float(item["low"]),
                "close": float(item["close"]),
            })
        except Exception:
            continue
    candles.sort(key=lambda candle: candle["time"])
    current_minute_start = int(time.time() // 60 * 60)
    closed = [candle for candle in candles if candle["time"] < current_minute_start]
    return closed if len(closed) >= RSI_LENGTH + 3 else None


# =====================================================
# STRATEGY
# =====================================================

def calculate_qty(entry_price):
    if entry_price <= 0:
        return 0
    return int(CAPITAL / entry_price)


def candle_is_red(candle):
    return candle["close"] < candle["open"]


def candle_is_green(candle):
    return candle["close"] > candle["open"]


def calculate_rsi(closes, length=RSI_LENGTH):
    if len(closes) < length + 1:
        return None
    gains  = []
    losses = []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0))
        losses.append(abs(min(change, 0)))
    avg_gain = sum(gains[:length]) / length
    avg_loss = sum(losses[:length]) / length
    for i in range(length, len(gains)):
        avg_gain = ((avg_gain * (length - 1)) + gains[i]) / length
        avg_loss = ((avg_loss * (length - 1)) + losses[i]) / length
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def check_setup(candles, ltp):
    if not candles or ltp is None or len(candles) < RSI_LENGTH + 3:
        return None
    red   = candles[-2]
    green = candles[-1]
    closes = [candle["close"] for candle in candles]
    rsi    = calculate_rsi(closes, RSI_LENGTH)
    red_ok      = candle_is_red(red)
    green_ok    = candle_is_green(green)
    breakout_ok = ltp > green["high"]
    rsi_ok      = rsi is not None and rsi > RSI_MIN
    if not (red_ok and green_ok and breakout_ok and rsi_ok):
        return None
    stop_loss = min(red["low"], green["low"])
    entry = ltp
    return {
        "signal_id":  f"{red['time']}:{green['time']}",
        "entry":      entry,
        "target":     round(entry * (1 + TARGET_PERCENT / 100), 2),
        "stop_loss":  float(stop_loss),
        "green_high": float(green["high"]),
        "rsi":        rsi,
        "red":        red,
        "green":      green,
    }


def setup_status(candles, ltp):
    if candles is None:
        return status_unavailable("candles unavailable")
    if ltp is None:
        return status_unavailable("ltp unavailable")
    if len(candles) < RSI_LENGTH + 3:
        return status_unavailable("not enough closed candles")

    red   = candles[-2]
    green = candles[-1]
    closes = [candle["close"] for candle in candles]
    rsi    = calculate_rsi(closes, RSI_LENGTH)

    red_ok      = candle_is_red(red)
    green_ok    = candle_is_green(green)
    breakout_ok = ltp > green["high"]
    rsi_ok      = rsi is not None and rsi > RSI_MIN
    setup_ok    = red_ok and green_ok and breakout_ok and rsi_ok
    stop_loss   = min(red["low"], green["low"])
    target_if_entry = round(ltp * (1 + TARGET_PERCENT / 100), 2)

    if setup_ok:
        reason = "all conditions met"
    elif not red_ok:
        reason = "previous-to-previous closed candle is not red"
    elif not green_ok:
        reason = "previous closed candle is not green"
    elif not breakout_ok:
        reason = "LTP has not broken previous green high"
    elif not rsi_ok:
        reason = f"RSI({RSI_LENGTH}) is not above {RSI_MIN}"
    else:
        reason = "waiting"

    return {
        "ok":             True,
        "setup_ok":       setup_ok,
        "ltp":            ltp,
        "rsi":            rsi,
        "qty":            calculate_qty(ltp),
        "red_ok":         red_ok,
        "green_ok":       green_ok,
        "breakout_ok":    breakout_ok,
        "rsi_ok":         rsi_ok,
        "green_high":     green["high"],
        "stop_loss":      stop_loss,
        "target_if_entry":target_if_entry,
        "reason":         reason,
    }


# =====================================================
# TRADE LOG
# =====================================================

def save_trade(row):
    header = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(row.keys()))
        if header:
            writer.writeheader()
        writer.writerow(row)


# =====================================================
# MAIN LOOP
# =====================================================

def main():
    global position

    log("BOT", (
        f"{SEP_THICK}\n"
        f"  {_b(_c(CYAN,'BTC ATM OPTIONS BOT STARTED'))}\n"
        f"{SEP_THIN}\n"
        f"  {_c(GRAY,'Timeframe :')} {TIMEFRAME}\n"
        f"  {_c(GRAY,'Candles   :')} {CANDLE_PRICE_SOURCE}\n"
        f"  {_c(GRAY,'Live price:')} {LIVE_PRICE_SOURCE}\n"
        f"  {_c(GRAY,'Capital   :')} {CAPITAL}\n"
        f"  {_c(GRAY,'Target    :')} {TARGET_PERCENT}%\n"
        f"  {_c(GRAY,'RSI       :')} length {RSI_LENGTH}, must be above {RSI_MIN}\n"
        f"  {_c(GRAY,'DRY_RUN   :')} {_c(YELLOW if DRY_RUN else GREEN, str(DRY_RUN))}\n"
        f"  {_c(GRAY,'Paper if live order fails:')} {PAPER_ON_ORDER_FAIL}\n"
        f"{SEP_THICK}"
    ))

    atm_call, atm_put = find_atm_contracts()
    watchlist = [contract for contract in [atm_call, atm_put] if contract]

    if not watchlist:
        log("ERROR", "No ATM contracts found. Exiting.")
        return

    watching = [
        f"  {side_dot(side_of(c))} {side_of(c):<5} {c.get('symbol')}"
        for c in watchlist
    ]
    log("BOT", f"Watching:\n" + "\n".join(watching))

    last_status_time = 0
    status_by_key    = {}

    while True:
        try:
            for contract in watchlist:
                symbol     = contract.get("symbol")
                product_id = contract.get("id")
                side       = side_of(contract)
                status_key = f"{side} {symbol}"

                if not symbol or not product_id:
                    continue

                candles = get_candles(symbol)
                ltp     = get_ltp(symbol) if candles is not None else None

                if position is None:
                    status_by_key[status_key] = setup_status(candles, ltp)
                    setup = check_setup(candles, ltp)

                    if not setup:
                        continue

                    if last_signal_id_by_symbol.get(symbol) == setup["signal_id"]:
                        continue

                    qty = calculate_qty(setup["entry"])
                    if qty <= 0:
                        status_by_key[status_key] = status_unavailable("qty is zero")
                        continue

                    print_trade_found(side, symbol, setup, qty)

                    order       = place_order(product_id, "buy", qty)
                    last_signal_id_by_symbol[symbol] = setup["signal_id"]

                    paper_trade = False

                    if not order:
                        if not PAPER_ON_ORDER_FAIL:
                            continue
                        paper_trade = True
                        log("PAPER", (
                            "Live BUY was not placed, so tracking this signal as PAPER trade.\n"
                            "PnL will be calculated from live LTP until target or stoploss."
                        ))
                        print_order("BUY", side, symbol, product_id, qty, setup["entry"], paper=True)
                    else:
                        paper_trade = bool(order.get("dry_run"))
                        print_order("BUY", side, symbol, product_id, qty, setup["entry"],
                                    dry_run=bool(order.get("dry_run")))

                    position = {
                        "side":       side,
                        "symbol":     symbol,
                        "product_id": product_id,
                        "entry":      setup["entry"],
                        "target":     setup["target"],
                        "stop_loss":  setup["stop_loss"],
                        "qty":        qty,
                        "rsi":        setup["rsi"],
                        "signal_id":  setup["signal_id"],
                        "paper":      paper_trade,
                        "entry_time": now_text(),
                    }
                    print_pnl_status(position, setup["entry"])
                    continue

                if position["symbol"] != symbol:
                    continue

                if ltp is None:
                    ltp = get_ltp(symbol)
                if ltp is None:
                    continue

                exit_reason = None
                if ltp >= position["target"]:
                    exit_reason = "TARGET"
                elif ltp <= position["stop_loss"]:
                    exit_reason = "STOPLOSS"

                if not exit_reason:
                    continue

                pnl, pnl_percent = pnl_values(position, ltp)

                if position.get("paper"):
                    sell_order = {"success": True, "paper": True}
                else:
                    sell_order = place_order(position["product_id"], "sell", position["qty"])
                    if not sell_order:
                        log("ERROR", f"Exit signal {exit_reason}, but SELL failed.\nPnL at signal: {fmt(pnl)} ({fmt(pnl_percent)}%)")
                        continue

                print_order(
                    "SELL",
                    position["side"],
                    symbol,
                    position["product_id"],
                    position["qty"],
                    ltp,
                    dry_run=bool(sell_order.get("dry_run")),
                    paper=bool(sell_order.get("paper")),
                )
                print_trade_result(exit_reason, position, ltp)

                save_trade({
                    "symbol":     position["symbol"],
                    "side":       position["side"],
                    "entry":      position["entry"],
                    "exit":       ltp,
                    "qty":        position["qty"],
                    "rsi":        position["rsi"],
                    "pnl":        pnl,
                    "pnl_percent":pnl_percent,
                    "mode":       "PAPER" if position.get("paper") else "LIVE",
                    "reason":     exit_reason,
                    "entry_time": position["entry_time"],
                    "exit_time":  now_text(),
                })

                position = None

            if time.time() - last_status_time >= STATUS_INTERVAL:
                if position is None:
                    print_watch_status(status_by_key)
                else:
                    ltp = get_ltp(position["symbol"])
                    if ltp is not None:
                        print_pnl_status(position, ltp)
                last_status_time = time.time()

            time.sleep(LOOP_INTERVAL)

        except KeyboardInterrupt:
            log("BOT", "Stopped by user.")
            print_shutdown_summary()
            break
        except Exception as exc:
            log("ERROR", f"Main loop error:\n{exc}")
            time.sleep(LOOP_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("ERROR", f"Bot stopped unexpectedly:\n{exc}")
        input("Press Enter to close...")