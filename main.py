import os
import time
import logging
import requests
from datetime import datetime

# ============================================================
# FOREX V2 SIGNAL BOT
# SIGNAL ONLY — NO AUTOMATIC TRADING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

# ---------------- CONFIG ----------------

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY", "")

SYMBOLS = [
    x.strip()
    for x in os.getenv(
        "SYMBOLS",
        "EUR/USD,GBP/USD,USD/JPY,XAU/USD"
    ).split(",")
]

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "300"))
MIN_SCORE = float(os.getenv("MIN_SCORE", "75"))

ATR_SL_MULTIPLIER = float(
    os.getenv("ATR_SL_MULTIPLIER", "1.5")
)

ATR_TP_MULTIPLIER = float(
    os.getenv("ATR_TP_MULTIPLIER", "2.5")
)

LOOKBACK = int(os.getenv("LOOKBACK", "150"))

session = requests.Session()
session.headers.update({
    "User-Agent": "Forex-V2-Signal-Bot/2.0"
})

last_signals = {}


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage"
    )

    response = session.post(
        url,
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message
        },
        timeout=20
    )

    response.raise_for_status()

    return response.json()


# ============================================================
# MARKET DATA
# ============================================================

def get_candles(symbol, interval):

    url = "https://api.twelvedata.com/time_series"

    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": LOOKBACK,
        "apikey": TWELVE_DATA_API_KEY,
        "format": "JSON",
        "timezone": "UTC"
    }

    max_retries = 3

    for attempt in range(max_retries):

        try:

            response = session.get(
                url,
                params=params,
                timeout=20
            )

            # Twelve Data rate limit
            if response.status_code == 429:

                wait_seconds = 60 * (attempt + 1)

                logging.warning(
                    f"Twelve Data rate limit for "
                    f"{symbol} {interval}. "
                    f"Waiting {wait_seconds}s..."
                )

                time.sleep(wait_seconds)
                continue

            response.raise_for_status()

            data = response.json()

            if "values" not in data:

                raise RuntimeError(
                    data.get(
                        "message",
                        f"No data returned for {symbol}"
                    )
                )

            candles = []

            # Twelve Data normally returns newest first.
            # Reverse so calculations run oldest -> newest.

            for item in reversed(data["values"]):

                candles.append({
                    "time": item["datetime"],
                    "open": float(item["open"]),
                    "high": float(item["high"]),
                    "low": float(item["low"]),
                    "close": float(item["close"])
                })

            return candles

        except requests.RequestException as e:

            if attempt == max_retries - 1:
                raise

            wait_seconds = 30 * (attempt + 1)

            logging.warning(
                f"Request error for {symbol} {interval}: "
                f"{e}. Retrying in {wait_seconds}s..."
            )

            time.sleep(wait_seconds)

    raise RuntimeError(
        f"Unable to retrieve candles for {symbol} {interval}"
    )


# ============================================================
# EMA
# ============================================================

def ema(values, period):

    if len(values) < period:
        return None

    multiplier = 2 / (period + 1)

    result = sum(values[:period]) / period

    for price in values[period:]:

        result = (
            price * multiplier
            +
            result * (1 - multiplier)
        )

    return result


# ============================================================
# RSI
# ============================================================

def calculate_rsi(values, period=14):

    if len(values) < period + 1:
        return None

    gains = []
    losses = []

    for i in range(1, period + 1):

        change = values[i] - values[i - 1]

        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    average_gain = sum(gains) / period
    average_loss = sum(losses) / period

    for i in range(period + 1, len(values)):

        change = values[i] - values[i - 1]

        gain = max(change, 0)
        loss = max(-change, 0)

        average_gain = (
            (average_gain * (period - 1) + gain)
            / period
        )

        average_loss = (
            (average_loss * (period - 1) + loss)
            / period
        )

    if average_loss == 0:
        return 100

    relative_strength = (
        average_gain / average_loss
    )

    return 100 - (
        100 / (1 + relative_strength)
    )


# ============================================================
# ATR
# ============================================================

def calculate_atr(candles, period=14):

    if len(candles) < period + 1:
        return None

    true_ranges = []

    for i in range(1, len(candles)):

        high = candles[i]["high"]
        low = candles[i]["low"]
        previous_close = candles[i - 1]["close"]

        true_range = max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close)
        )

        true_ranges.append(true_range)

    return sum(true_ranges[-period:]) / period


# ============================================================
# MACD
# ============================================================

def calculate_macd(values):

    fast_period = 12
    slow_period = 26
    signal_period = 9

    if len(values) < slow_period + signal_period:
        return None, None

    def ema_series(data, period):

        if len(data) < period:
            return []

        multiplier = 2 / (period + 1)

        current = sum(data[:period]) / period

        output = [None] * (period - 1)
        output.append(current)

        for value in data[period:]:

            current = (
                value * multiplier
                +
                current * (1 - multiplier)
            )

            output.append(current)

        return output

    fast = ema_series(values, fast_period)
    slow = ema_series(values, slow_period)

    macd_values = []

    for i in range(len(values)):

        if (
            fast[i] is not None
            and slow[i] is not None
        ):

            macd_values.append(
                fast[i] - slow[i]
            )

    if len(macd_values) < signal_period:
        return None, None

    signal = ema(
        macd_values,
        signal_period
    )

    return macd_values[-1], signal


# ============================================================
# TREND
# ============================================================

def calculate_trend(candles):

    closes = [
        candle["close"]
        for candle in candles
    ]

    fast = ema(closes, 20)
    medium = ema(closes, 50)
    slow = ema(closes, 200)

    if None in (fast, medium, slow):
        return 0

    if fast > medium > slow:
        return 1

    if fast < medium < slow:
        return -1

    return 0


# ============================================================
# SWING POINTS
# ============================================================

def find_swings(candles):

    swing_highs = []
    swing_lows = []

    left = 2
    right = 2

    for i in range(
        left,
        len(candles) - right
    ):

        high = candles[i]["high"]
        low = candles[i]["low"]

        is_high = True
        is_low = True

        for j in range(
            i - left,
            i + right + 1
        ):

            if j == i:
                continue

            if candles[j]["high"] >= high:
                is_high = False

            if candles[j]["low"] <= low:
                is_low = False

        if is_high:
            swing_highs.append(
                (i, high)
            )

        if is_low:
            swing_lows.append(
                (i, low)
            )

    return swing_highs, swing_lows


# ============================================================
# MARKET STRUCTURE
# ============================================================

def market_structure(candles):

    highs, lows = find_swings(candles)

    if len(highs) < 2 or len(lows) < 2:
        return {
            "direction": 0,
            "bos": False,
            "choch": False,
            "swing_high": None,
            "swing_low": None
        }

    last = candles[-1]

    previous_high = highs[-2][1]
    latest_high = highs[-1][1]

    previous_low = lows[-2][1]
    latest_low = lows[-1][1]

    bullish_structure = (
        latest_high > previous_high
        and
        latest_low > previous_low
    )

    bearish_structure = (
        latest_high < previous_high
        and
        latest_low < previous_low
    )

    bullish_bos = (
        last["close"] > latest_high
    )

    bearish_bos = (
        last["close"] < latest_low
    )

    direction = 0

    if bullish_structure:
        direction = 1

    elif bearish_structure:
        direction = -1

    if bullish_bos:
        direction = 1

    elif bearish_bos:
        direction = -1

    return {
        "direction": direction,
        "bos": bullish_bos or bearish_bos,
        "choch": (
            (bullish_bos and bearish_structure)
            or
            (bearish_bos and bullish_structure)
        ),
        "swing_high": latest_high,
        "swing_low": latest_low
    }


# ============================================================
# LIQUIDITY SWEEP
# ============================================================

def liquidity_sweep(candles, structure):

    if not structure["swing_high"]:
        return 0

    if not structure["swing_low"]:
        return 0

    last = candles[-1]

    swing_high = structure["swing_high"]
    swing_low = structure["swing_low"]

    # Sell-side liquidity sweep:
    # price runs below low then closes back above it.

    bullish_sweep = (
        last["low"] < swing_low
        and
        last["close"] > swing_low
    )

    # Buy-side liquidity sweep:
    # price runs above high then closes back below it.

    bearish_sweep = (
        last["high"] > swing_high
        and
        last["close"] < swing_high
    )

    if bullish_sweep:
        return 1

    if bearish_sweep:
        return -1

    return 0


# ============================================================
# CANDLE CONFIRMATION
# ============================================================

def candle_confirmation(candles):

    if len(candles) < 3:
        return 0

    previous = candles[-2]
    current = candles[-1]

    body = abs(
        current["close"]
        -
        current["open"]
    )

    upper_wick = (
        current["high"]
        -
        max(
            current["open"],
            current["close"]
        )
    )

    lower_wick = (
        min(
            current["open"],
            current["close"]
        )
        -
        current["low"]
    )

    bullish_engulfing = (
        previous["close"] < previous["open"]
        and
        current["close"] > current["open"]
        and
        current["close"] > previous["open"]
        and
        current["open"] < previous["close"]
    )

    bearish_engulfing = (
        previous["close"] > previous["open"]
        and
        current["close"] < current["open"]
        and
        current["open"] > previous["close"]
        and
        current["close"] < previous["open"]
    )

    bullish_pin = (
        lower_wick > body * 2
        and
        lower_wick > upper_wick * 1.5
    )

    bearish_pin = (
        upper_wick > body * 2
        and
        upper_wick > lower_wick * 1.5
    )

    if bullish_engulfing or bullish_pin:
        return 1

    if bearish_engulfing or bearish_pin:
        return -1

    return 0


# ============================================================
# SUPPORT / RESISTANCE
# ============================================================

def support_resistance(candles):

    recent = candles[-50:]

    support = min(
        candle["low"]
        for candle in recent
    )

    resistance = max(
        candle["high"]
        for candle in recent
    )

    return support, resistance


# ============================================================
# ANALYSIS
# ============================================================

def analyze(symbol):

    m15 = get_candles(
        symbol,
        "15min"
    )

    h1 = get_candles(
        symbol,
        "1h"
    )

    h4 = get_candles(
        symbol,
        "4h"
    )

    closes = [
        candle["close"]
        for candle in m15
    ]

    trend_m15 = calculate_trend(m15)
    trend_h1 = calculate_trend(h1)
    trend_h4 = calculate_trend(h4)

    rsi = calculate_rsi(closes)

    macd_value, macd_signal = calculate_macd(
        closes
    )

    atr = calculate_atr(m15)

    structure = market_structure(m15)

    liquidity = liquidity_sweep(
        m15,
        structure
    )

    candle = candle_confirmation(
        m15
    )

    support, resistance = (
        support_resistance(m15)
    )

    if None in (
        rsi,
        macd_value,
        macd_signal,
        atr
    ):
        return None

    buy_score = 0
    sell_score = 0

    buy_reasons = []
    sell_reasons = []

    # ---------------------------------------------------------
    # H4
    # ---------------------------------------------------------

    if trend_h4 == 1:
        buy_score += 25
        buy_reasons.append(
            "H4 bullish EMA alignment"
        )

    elif trend_h4 == -1:
        sell_score += 25
        sell_reasons.append(
            "H4 bearish EMA alignment"
        )

    # ---------------------------------------------------------
    # H1
    # ---------------------------------------------------------

    if trend_h1 == 1:
        buy_score += 20
        buy_reasons.append(
            "H1 bullish EMA alignment"
        )

    elif trend_h1 == -1:
        sell_score += 20
        sell_reasons.append(
            "H1 bearish EMA alignment"
        )

    # ---------------------------------------------------------
    # M15
    # ---------------------------------------------------------

    if trend_m15 == 1:
        buy_score += 10
        buy_reasons.append(
            "M15 bullish trend"
        )

    elif trend_m15 == -1:
        sell_score += 10
        sell_reasons.append(
            "M15 bearish trend"
        )

    # ---------------------------------------------------------
    # RSI
    # ---------------------------------------------------------

    if rsi >= 55:

        buy_score += 10

        buy_reasons.append(
            f"RSI bullish ({rsi:.1f})"
        )

    elif rsi <= 45:

        sell_score += 10

        sell_reasons.append(
            f"RSI bearish ({rsi:.1f})"
        )

    # ---------------------------------------------------------
    # MACD
    # ---------------------------------------------------------

    if macd_value > macd_signal:

        buy_score += 10

        buy_reasons.append(
            "MACD bullish"
        )

    elif macd_value < macd_signal:

        sell_score += 10

        sell_reasons.append(
            "MACD bearish"
        )

    # ---------------------------------------------------------
    # MARKET STRUCTURE
    # ---------------------------------------------------------

    if structure["direction"] == 1:

        buy_score += 10

        buy_reasons.append(
            "Bullish market structure"
        )

    elif structure["direction"] == -1:

        sell_score += 10

        sell_reasons.append(
            "Bearish market structure"
        )

    # ---------------------------------------------------------
    # BOS
    # ---------------------------------------------------------

    if structure["bos"]:

        if structure["direction"] == 1:

            buy_score += 10

            buy_reasons.append(
                "Bullish BOS"
            )

        elif structure["direction"] == -1:

            sell_score += 10

            sell_reasons.append(
                "Bearish BOS"
            )

    # ---------------------------------------------------------
    # CHOCH
    # ---------------------------------------------------------

    if structure["choch"]:

        if structure["direction"] == 1:

            buy_score += 5

            buy_reasons.append(
                "Bullish CHOCH"
            )

        elif structure["direction"] == -1:

            sell_score += 5

            sell_reasons.append(
                "Bearish CHOCH"
            )

    # ---------------------------------------------------------
    # LIQUIDITY
    # ---------------------------------------------------------

    if liquidity == 1:

        buy_score += 10

        buy_reasons.append(
            "Sell-side liquidity sweep"
        )

    elif liquidity == -1:

        sell_score += 10

        sell_reasons.append(
            "Buy-side liquidity sweep"
        )

    # ---------------------------------------------------------
    # CANDLE
    # ---------------------------------------------------------

    if candle == 1:

        buy_score += 5

        buy_reasons.append(
            "Bullish candle confirmation"
        )

    elif candle == -1:

        sell_score += 5

        sell_reasons.append(
            "Bearish candle confirmation"
        )

    # ---------------------------------------------------------
    # FINAL SIGNAL
    # ---------------------------------------------------------

    direction = 0

    if (
        buy_score >= MIN_SCORE
        and
        buy_score > sell_score
    ):
        direction = 1

    elif (
        sell_score >= MIN_SCORE
        and
        sell_score > buy_score
    ):
        direction = -1

    if direction == 0:

        return {
            "symbol": symbol,
            "signal": "WAIT",
            "buy_score": buy_score,
            "sell_score": sell_score
        }

    score = (
        buy_score
        if direction == 1
        else sell_score
    )

    last = m15[-1]

    entry = last["close"]

    # ---------------------------------------------------------
    # STOP / TARGET
    # ---------------------------------------------------------

    if direction == 1:

        if structure["swing_low"]:

            structural_sl = (
                structure["swing_low"]
                -
                atr * 0.15
            )

            atr_sl = (
                entry
                -
                atr * ATR_SL_MULTIPLIER
            )

            sl = min(
                structural_sl,
                atr_sl
            )

        else:

            sl = (
                entry
                -
                atr * ATR_SL_MULTIPLIER
            )

        tp = (
            entry
            +
            atr * ATR_TP_MULTIPLIER
        )

    else:

        if structure["swing_high"]:

            structural_sl = (
                structure["swing_high"]
                +
                atr * 0.15
            )

            atr_sl = (
                entry
                +
                atr * ATR_SL_MULTIPLIER
            )

            sl = max(
                structural_sl,
                atr_sl
            )

        else:

            sl = (
                entry
                +
                atr * ATR_SL_MULTIPLIER
            )

        tp = (
            entry
            -
            atr * ATR_TP_MULTIPLIER
        )

    risk = abs(
        entry - sl
    )

    reward = abs(
        tp - entry
    )

    rr = (
        reward / risk
        if risk > 0
        else 0
    )

    side = (
        "BUY"
        if direction == 1
        else "SELL"
    )

    reasons = (
        buy_reasons
        if direction == 1
        else sell_reasons
    )

    signal_key = (
        f"{symbol}|{side}|"
        f"{last['time']}"
    )

    return {
        "symbol": symbol,
        "signal": side,
        "score": score,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "rr": rr,
        "rsi": rsi,
        "atr": atr,
        "h4": trend_h4,
        "h1": trend_h1,
        "m15": trend_m15,
        "structure": structure,
        "liquidity": liquidity,
        "candle": candle,
        "reasons": reasons,
        "key": signal_key,
        "time": last["time"]
    }


# ============================================================
# PRICE FORMAT
# ============================================================

def format_price(symbol, value):

    if "JPY" in symbol:
        return f"{value:.3f}"

    if "XAU" in symbol:
        return f"{value:.2f}"

    return f"{value:.5f}"


# ============================================================
# TELEGRAM MESSAGE
# ============================================================

def create_message(result):

    icon = (
        "🟢"
        if result["signal"] == "BUY"
        else "🔴"
    )

    reasons = "\n".join(
        f"✅ {reason}"
        for reason in result["reasons"]
    )

    return (
        f"{icon} V2 HIGH-CONFLUENCE SIGNAL\n\n"

        f"PAIR: {result['symbol']}\n"
        f"DIRECTION: {result['signal']}\n"
        f"SCORE: {result['score']:.0f}/100\n\n"

        f"ENTRY: "
        f"{format_price(result['symbol'], result['entry'])}\n"

        f"STOP LOSS: "
        f"{format_price(result['symbol'], result['sl'])}\n"

        f"TAKE PROFIT: "
        f"{format_price(result['symbol'], result['tp'])}\n"

        f"R:R: 1:{result['rr']:.2f}\n\n"

        f"MARKET ANALYSIS\n"
        f"{reasons}\n\n"

        f"RSI: {result['rsi']:.1f}\n"
        f"H4/H1/M15: "
        f"{result['h4']}/"
        f"{result['h1']}/"
        f"{result['m15']}\n"

        f"\nSignal candle: {result['time']}\n\n"

        f"⚠️ SIGNAL ONLY\n"
        f"No automatic trade executed."
    )


# ============================================================
# MAIN LOOP
# ============================================================

def main():

    if not TELEGRAM_TOKEN:

        raise SystemExit(
            "Missing TELEGRAM_BOT_TOKEN"
        )

    if not TELEGRAM_CHAT_ID:

        raise SystemExit(
            "Missing TELEGRAM_CHAT_ID"
        )

    if not TWELVE_DATA_API_KEY:

        raise SystemExit(
            "Missing TWELVE_DATA_API_KEY"
        )

    logging.info(
        "Forex V2 Signal Engine started."
    )

    logging.info(
        "Symbols: %s",
        ", ".join(SYMBOLS)
    )

    logging.info(
        "Minimum score: %s",
        MIN_SCORE
    )

    while True:

        for symbol in SYMBOLS:

            try:

                result = analyze(symbol)

                if not result:
                    continue

                if result["signal"] not in (
                    "BUY",
                    "SELL"
                ):
                    logging.info(
                        "%s: WAIT | BUY %.0f | SELL %.0f",
                        symbol,
                        result["buy_score"],
                        result["sell_score"]
                    )

                    continue

                if (
                    last_signals.get(symbol)
                    ==
                    result["key"]
                ):
                    continue

                message = create_message(
                    result
                )

                send_telegram(
                    message
                )

                last_signals[symbol] = (
                    result["key"]
                )

                logging.info(
                    "Sent %s signal for %s",
                    result["signal"],
                    symbol
                )

            except Exception as error:

                logging.exception(
                    "Error analyzing %s: %s",
                    symbol,
                    error
                )

        time.sleep(
            POLL_SECONDS
        )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    main()
