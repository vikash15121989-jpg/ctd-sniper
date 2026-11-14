from datetime import datetime
import json
import os
import gspread
import numpy as np
import pandas as pd
import pytz
import yfinance as yf

IST = pytz.timezone("Asia/Kolkata")
print("=== CTD_SNIPER MONTHLY MAJOR SWING BACKTEST ===")

# -------------------------------------------------------------
# 1. GOOGLE SHEETS CONNECTION & WATCHLIST LOAD
# -------------------------------------------------------------
try:
  gc = gspread.service_account_from_dict(json.loads(os.environ["GSHEET_KEY"]))
  sh = gc.open("CTD_Sniper")
  raw = sh.worksheet("Watchlist").col_values(1)
  STOCKS = [
      s.strip().upper()
      for s in raw
      if s and len(s.strip()) > 2 and s.upper() not in ["STOCK", "SYMBOL"]
  ]
  print(f"✅ Loaded {len(STOCKS)} stocks from Watchlist.")
except Exception as e:
  print(f"❌ Google Sheet Connection Error: {e}")
  STOCKS = []


# -------------------------------------------------------------
# 2. MONTHLY MAJOR SWING STRATEGY FUNCTION
# -------------------------------------------------------------
def backtest_monthly_major_swing(
    symbol,
    stop_loss_pct=0.07,
    take_profit_pct=0.14,
):
  trades = []
  try:
    ticker_symbol = f"{symbol}.NS" if not symbol.endswith(".NS") else symbol
    df = yf.download(
        ticker_symbol,
        start="2012-01-01",
        interval="1mo",
        auto_adjust=False,
        progress=False,
    )

    if df is None or df.empty or len(df) < 40:
      return trades

    if isinstance(df.columns, pd.MultiIndex):
      df.columns = df.columns.get_level_values(0)

    df.dropna(subset=["Open", "High", "Low", "Close"], inplace=True)
    if "Volume" not in df.columns:
      df["Volume"] = 100000

    highs = df["High"].values
    lows = df["Low"].values
    closes = df["Close"].values
    opens = df["Open"].values
    volumes = df["Volume"].values
    dates = df.index
    n = len(df)

    df["Range_Pct"] = ((df["High"] - df["Low"]) / df["Close"]) * 100
    ranges = df["Range_Pct"].values
    min_consolidation_months = 3

    in_trade = False
    pending_entry = False
    entry_price = 0
    stop_loss = 0
    take_profit = 0
    entry_date = None
    resistance_level = 0
    max_price_reached = 0

    start_idx = min_consolidation_months * 2 + 5

    for i in range(start_idx, n):
      current_date = dates[i]
      curr_close = closes[i]
      curr_high = highs[i]
      curr_low = lows[i]
      curr_open = opens[i]
      curr_range = ranges[i]

      if in_trade:
        if curr_high > max_price_reached:
          max_price_reached = curr_high

        if curr_low <= stop_loss or curr_high >= take_profit:
          is_win = curr_high >= take_profit
          pnl_pct = (
              (take_profit - entry_price) / entry_price * 100
              if is_win
              else (stop_loss - entry_price) / entry_price * 100
          )
          max_profit_pct = (
              (max_price_reached - entry_price) / entry_price
          ) * 100

          if entry_date.year in [2025, 2026] or current_date.year in [
              2025,
              2026,
          ]:
            tv_url = f"https://www.tradingview.com/chart/?symbol=NSE:{symbol}"
            sheet_link_formula = f'=HYPERLINK("{tv_url}", "{symbol} Chart")'

            trades.append({
                "Symbol": symbol,
                "Breakout_Month": entry_date.strftime("%b %Y"),
                "Resistance": round(resistance_level, 2),
                "Entry_Price": round(entry_price, 2),
                "Max_Profit_%": round(max_profit_pct, 2),
                "Result": "WIN" if is_win else "LOSS",
                "PnL_%": round(pnl_pct, 2),
                "TradingView_Chart": sheet_link_formula,
            })
            
