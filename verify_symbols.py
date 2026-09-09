#!/usr/bin/env python3
"""
Run this BEFORE trusting the bot. Checks that every symbol in
telegram_stock_bot.py actually returns real price data for the
timeframe you plan to use. If a symbol fails here, it will also
fail (or silently be skipped) in the bot — fix it here first.
"""
import yfinance as yf
from telegram_stock_bot import SYMBOLS, TIMEFRAME_CONFIG

def main():
    print(f"Checking {len(SYMBOLS)} symbols across all timeframes...\n")
    any_failure = False

    for timeframe, cfg in TIMEFRAME_CONFIG.items():
        print(f"── {timeframe.upper()} (interval={cfg['interval']}, period={cfg['period']}) ──")
        for name, ticker in SYMBOLS.items():
            try:
                data = yf.Ticker(ticker).history(period=cfg["period"], interval=cfg["interval"])
                if data is None or data.empty:
                    print(f"  ❌ {name:8s} ({ticker}): NO DATA RETURNED")
                    any_failure = True
                else:
                    last_price = data["Close"].iloc[-1]
                    print(f"  ✅ {name:8s} ({ticker}): {len(data)} bars, last close = {last_price:.5f}")
            except Exception as e:
                print(f"  ❌ {name:8s} ({ticker}): ERROR — {e}")
                any_failure = True
        print()

    if any_failure:
        print("⚠️  Some symbols failed. Do NOT trust the bot for those symbols")
        print("    until you fix the ticker or remove them from SYMBOLS.")
    else:
        print("✅ All symbols returned data on all timeframes.")

if __name__ == "__main__":
    main()
